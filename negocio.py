import asyncio
import math
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Asegurar que el directorio Backend esté en sys.path para soportar ejecución
# tanto desde 'Backend/' como desde la raíz del proyecto ('Backend.negocio:app')
BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware

from database import products_collection, orders_collection, customers_collection, sales_collection
from auth import get_current_user

app = FastAPI(title="Analytics & Business API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PRODUCT_PRICE_FIELDS = ("price", "precio", "unit_price", "sale_price", "amount", "gross_sales")
ORDER_TOTAL_FIELDS = ("gross_sales",)
PRODUCT_RATING_FIELDS = ("product_rating", "rating", "rating_value")


def numeric_value(record: dict, field_names: tuple[str, ...]):
    for field in field_names:
        value = record.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if math.isfinite(value):
                return value
    return None


def summarize_values(records: list[dict], field_names: tuple[str, ...]) -> dict:
    values = [
        value
        for record in records
        if (value := numeric_value(record, field_names)) is not None
    ]
    if not values:
        return {
            "analyzed_count": 0,
            "sum": 0,
            "average": 0,
            "minimum": 0,
            "maximum": 0,
        }
    return {
        "analyzed_count": len(values),
        "sum": round(sum(values), 2),
        "average": round(sum(values) / len(values), 2),
        "minimum": round(min(values), 2),
        "maximum": round(max(values), 2),
    }


def count_by_field(records: list[dict], field_name: str, label: str) -> list[dict]:
    counts = {}
    for record in records:
        value = record.get(field_name)
        key = str(value).strip() if value is not None and str(value).strip() else "Sin especificar"
        counts[key] = counts.get(key, 0) + 1
    return [
        {label: name, "count": count}
        for name, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)
    ]


def get_monthly_order_counts(orders: list[dict]) -> list[dict]:
    counts = {}
    for order in orders:
        value = order.get("order_date")
        if not value:
            continue
        try:
            month = datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%Y-%m")
        except ValueError:
            continue
        counts[month] = counts.get(month, 0) + 1

    if not counts:
        return []

    first_month = date.fromisoformat(f"{min(counts)}-01")
    last_month = date.fromisoformat(f"{max(counts)}-01")
    results = []
    current_month = first_month
    while current_month <= last_month:
        month = current_month.strftime("%Y-%m")
        results.append({"month": month, "orders_count": counts.get(month, 0)})
        next_month = current_month.month % 12 + 1
        next_year = current_month.year + (current_month.month == 12)
        current_month = date(next_year, next_month, 1)
    return results


def get_product_category_stats(products: list[dict]) -> list[dict]:
    groups = {}
    for product in products:
        category = product.get("product_category") or "Sin especificar"
        group = groups.setdefault(category, {"count": 0, "prices": [], "ratings": []})
        group["count"] += 1
        price = numeric_value(product, PRODUCT_PRICE_FIELDS)
        rating = numeric_value(product, PRODUCT_RATING_FIELDS)
        if price is not None:
            group["prices"].append(price)
        if rating is not None:
            group["ratings"].append(rating)

    results = []
    for category, group in groups.items():
        prices = group["prices"]
        ratings = group["ratings"]
        results.append(
            {
                "category": category,
                "product_count": group["count"],
                "average_price": round(sum(prices) / len(prices), 2) if prices else 0,
                "average_rating": round(sum(ratings) / len(ratings), 2) if ratings else 0,
            }
        )
    return sorted(results, key=lambda item: item["product_count"], reverse=True)


_DASHBOARD_CACHE: Dict[str, tuple[Dict[str, Any], float]] = {}
CACHE_TTL_SECONDS = 90


@app.get("/metrics/overview")
async def get_overview(current_user: Dict[str, Any] = Depends(get_current_user)):
    uid = current_user["uid"]
    now = time.time()
    if uid in _DASHBOARD_CACHE and now < _DASHBOARD_CACHE[uid][1]:
        return _DASHBOARD_CACHE[uid][0]["overview"]

    p_cnt, c_cnt, o_cnt, s_cnt = await asyncio.gather(
        products_collection.count_documents({"firebase_uid": uid}),
        customers_collection.count_documents({"firebase_uid": uid}),
        orders_collection.count_documents({"firebase_uid": uid}),
        sales_collection.count_documents({"firebase_uid": uid}),
    )
    return {
        "products_count": p_cnt,
        "customers_count": c_cnt,
        "orders_count": o_cnt,
        "sales_count": s_cnt,
    }


@app.get("/metrics/products-summary")
async def get_products_summary(current_user: Dict[str, Any] = Depends(get_current_user)):
    uid = current_user["uid"]
    now = time.time()
    if uid in _DASHBOARD_CACHE and now < _DASHBOARD_CACHE[uid][1]:
        return _DASHBOARD_CACHE[uid][0]["productsSummary"]

    products, total = await asyncio.gather(
        products_collection.find(
            {"firebase_uid": uid},
            {"_id": 0, "product_name": 1, "unit_price": 1, "product_category": 1, "product_rating": 1}
        ).sort("_id", -1).limit(100).to_list(100),
        products_collection.count_documents({"firebase_uid": uid}),
    )
    return {
        "total_products": total,
        "price": summarize_values(products, PRODUCT_PRICE_FIELDS),
    }


@app.get("/metrics/orders-summary")
async def get_orders_summary(current_user: Dict[str, Any] = Depends(get_current_user)):
    uid = current_user["uid"]
    now = time.time()
    if uid in _DASHBOARD_CACHE and now < _DASHBOARD_CACHE[uid][1]:
        return _DASHBOARD_CACHE[uid][0]["ordersSummary"]

    orders, total = await asyncio.gather(
        orders_collection.find(
            {"firebase_uid": uid},
            {"_id": 0, "gross_sales": 1, "order_date": 1, "order_status": 1, "sales_channel": 1}
        ).sort("order_date", -1).limit(150).to_list(150),
        orders_collection.count_documents({"firebase_uid": uid}),
    )
    return {
        "total_orders": total,
        "order_total": summarize_values(orders, ORDER_TOTAL_FIELDS),
    }


@app.get("/metrics/products-ranking")
async def get_products_ranking(
    limit: int = Query(default=10, ge=1, le=100),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    uid = current_user["uid"]
    now = time.time()
    if uid in _DASHBOARD_CACHE and now < _DASHBOARD_CACHE[uid][1]:
        return _DASHBOARD_CACHE[uid][0]["ranking"][:limit]

    products = await products_collection.find(
        {"firebase_uid": uid},
        {"_id": 0, "product_name": 1, "unit_price": 1, "name": 1}
    ).sort("_id", -1).limit(100).to_list(100)

    ranked_products = []
    for product in products:
        price = numeric_value(product, PRODUCT_PRICE_FIELDS)
        if price is not None:
            ranked_products.append((price, product))

    ranked_products.sort(key=lambda item: item[0], reverse=True)
    return [
        {
            "rank": index,
            "product_name": next(
                (
                    product[field]
                    for field in ("product_name", "product-name", "name", "nombre", "title")
                    if field in product
                ),
                None,
            ),
            "price": price,
        }
        for index, (price, product) in enumerate(ranked_products[:limit], start=1)
    ]


@app.get("/metrics/charts")
async def get_chart_metrics(current_user: Dict[str, Any] = Depends(get_current_user)):
    uid = current_user["uid"]
    now = time.time()
    if uid in _DASHBOARD_CACHE and now < _DASHBOARD_CACHE[uid][1]:
        return _DASHBOARD_CACHE[uid][0]["charts"]

    products, orders = await asyncio.gather(
        products_collection.find(
            {"firebase_uid": uid},
            {"_id": 0, "product_category": 1, "unit_price": 1, "product_rating": 1}
        ).sort("_id", -1).limit(100).to_list(100),
        orders_collection.find(
            {"firebase_uid": uid},
            {"_id": 0, "order_date": 1, "order_status": 1, "sales_channel": 1}
        ).sort("order_date", -1).limit(150).to_list(150),
    )
    return {
        "orders_by_month": get_monthly_order_counts(orders),
        "products_by_category": get_product_category_stats(products),
        "orders_by_status": count_by_field(orders, "order_status", "status"),
        "orders_by_channel": count_by_field(orders, "sales_channel", "channel"),
    }


@app.get("/metrics/sales-summary")
async def get_sales_summary(current_user: Dict[str, Any] = Depends(get_current_user)):
    uid = current_user["uid"]
    now = time.time()
    if uid in _DASHBOARD_CACHE and now < _DASHBOARD_CACHE[uid][1]:
        return _DASHBOARD_CACHE[uid][0].get("salesSummary", {
            "total_sales": 0,
            "active_sales_count": 0,
            "net_revenue": 0.0,
            "profit": 0.0,
            "average_ticket": 0.0,
        })

    sales, total_sales = await asyncio.gather(
        sales_collection.find(
            {"firebase_uid": uid},
            {"_id": 0, "financials": 1, "sale_status": 1, "status": 1, "net_sales": 1, "total": 1, "profit": 1}
        ).sort("_id", -1).limit(300).to_list(300),
        sales_collection.count_documents({"firebase_uid": uid}),
    )

    total_net_rev = 0.0
    total_prof = 0.0
    active_count = 0

    for s in sales:
        st = str(s.get("sale_status") or s.get("status") or "").upper()
        if st == "CANCELLED":
            continue
        fin = s.get("financials") or {}
        net_val = fin.get("net_revenue")
        if net_val is None:
            net_val = s.get("net_sales") or s.get("total") or 0.0
        prof_val = fin.get("profit")
        if prof_val is None:
            prof_val = s.get("profit") or 0.0
        try:
            n_f = float(net_val)
            p_f = float(prof_val)
            if math.isfinite(n_f):
                total_net_rev += n_f
            if math.isfinite(p_f):
                total_prof += p_f
            active_count += 1
        except (ValueError, TypeError):
            pass

    avg_ticket = round(total_net_rev / active_count, 2) if active_count > 0 else 0.0
    return {
        "total_sales": total_sales,
        "active_sales_count": active_count,
        "net_revenue": round(total_net_rev, 2),
        "profit": round(total_prof, 2),
        "average_ticket": avg_ticket,
    }


@app.get("/metrics/dashboard")
async def get_dashboard(
    force: bool = Query(default=False),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Endpoint consolidado de alto rendimiento para cargar el Dashboard completo.
    Utiliza proyecciones de campos mínimos, consultas paralelas con sort indexado
    y caché en memoria para responder en milisegundos sin sobrecargar MongoDB Atlas.
    """
    uid = current_user["uid"]
    now = time.time()

    if not force and uid in _DASHBOARD_CACHE:
        cached_data, expires_at = _DASHBOARD_CACHE[uid]
        if now < expires_at:
            return cached_data

    # Consultas ultrarrápidas con proyección de campos específicos y límites indexados
    p_cnt_task = products_collection.count_documents({"firebase_uid": uid})
    c_cnt_task = customers_collection.count_documents({"firebase_uid": uid})
    o_cnt_task = orders_collection.count_documents({"firebase_uid": uid})
    s_cnt_task = sales_collection.count_documents({"firebase_uid": uid})

    products_task = products_collection.find(
        {"firebase_uid": uid},
        {"_id": 0, "product_name": 1, "unit_price": 1, "product_category": 1, "product_rating": 1, "name": 1}
    ).sort("_id", -1).limit(100).to_list(100)

    orders_task = orders_collection.find(
        {"firebase_uid": uid},
        {"_id": 0, "gross_sales": 1, "order_date": 1, "order_status": 1, "sales_channel": 1}
    ).sort("order_date", -1).limit(150).to_list(150)

    sales_task = sales_collection.find(
        {"firebase_uid": uid},
        {"_id": 0, "financials": 1, "sale_status": 1, "status": 1, "net_sales": 1, "total": 1, "profit": 1}
    ).sort("_id", -1).limit(300).to_list(300)

    p_cnt, c_cnt, o_cnt, s_cnt, products, orders, sales = await asyncio.gather(
        p_cnt_task, c_cnt_task, o_cnt_task, s_cnt_task, products_task, orders_task, sales_task
    )

    ranked_products = []
    for product in products:
        price = numeric_value(product, PRODUCT_PRICE_FIELDS)
        if price is not None:
            ranked_products.append((price, product))

    ranked_products.sort(key=lambda item: item[0], reverse=True)

    ranking = [
        {
            "rank": index,
            "product_name": next(
                (
                    product[field]
                    for field in ("product_name", "product-name", "name", "nombre", "title")
                    if field in product
                ),
                None,
            ),
            "price": price,
        }
        for index, (price, product) in enumerate(ranked_products[:5], start=1)
    ]

    total_sales_net_revenue = 0.0
    total_sales_profit = 0.0
    active_sales_count = 0

    for s in sales:
        st = str(s.get("sale_status") or s.get("status") or "").upper()
        if st == "CANCELLED":
            continue
        fin = s.get("financials") or {}
        net_val = fin.get("net_revenue")
        if net_val is None:
            net_val = s.get("net_sales") or s.get("total") or 0.0
        prof_val = fin.get("profit")
        if prof_val is None:
            prof_val = s.get("profit") or 0.0

        try:
            n_f = float(net_val)
            p_f = float(prof_val)
            if math.isfinite(n_f):
                total_sales_net_revenue += n_f
            if math.isfinite(p_f):
                total_sales_profit += p_f
            active_sales_count += 1
        except (ValueError, TypeError):
            pass

    sales_avg_ticket = (
        round(total_sales_net_revenue / active_sales_count, 2)
        if active_sales_count > 0
        else 0.0
    )

    result = {
        "overview": {
            "products_count": p_cnt,
            "customers_count": c_cnt,
            "orders_count": o_cnt,
            "sales_count": s_cnt,
        },
        "productsSummary": {
            "total_products": p_cnt,
            "price": summarize_values(products, PRODUCT_PRICE_FIELDS),
        },
        "ordersSummary": {
            "total_orders": o_cnt,
            "order_total": summarize_values(orders, ORDER_TOTAL_FIELDS),
        },
        "salesSummary": {
            "total_sales": s_cnt,
            "active_sales_count": active_sales_count,
            "net_revenue": round(total_sales_net_revenue, 2),
            "profit": round(total_sales_profit, 2),
            "average_ticket": sales_avg_ticket,
        },
        "ranking": ranking,
        "charts": {
            "orders_by_month": get_monthly_order_counts(orders),
            "products_by_category": get_product_category_stats(products),
            "orders_by_status": count_by_field(orders, "order_status", "status"),
            "orders_by_channel": count_by_field(orders, "sales_channel", "channel"),
        },
    }

    _DASHBOARD_CACHE[uid] = (result, now + CACHE_TTL_SECONDS)
    return result