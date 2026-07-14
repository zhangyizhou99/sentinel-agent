"""Fixture app for evaluating Sentinel's discovery. | 评估 Sentinel 发现能力的样本应用。

EN: An UN-instrumented FastAPI service with several monitor-worthy paths: HTTP
    routes (RED), a redis cache, a database call, and an external payment API call.
    Ground truth for these lives in expected.json next to this file.
ZH: 一个未埋点的 FastAPI 服务，含多条值得监控的路径：HTTP 路由（RED）、redis 缓存、
    数据库调用、外部支付 API 调用。它们的标准答案在同目录 expected.json 里。
"""
import httpx
import redis
import sqlite3
from fastapi import FastAPI

app = FastAPI()
cache = redis.Redis()
db = sqlite3.connect("shop.db")


@app.get("/products/{product_id}")
def get_product(product_id: int):
    cached = cache.get(f"product:{product_id}")          # cache read
    if cached:
        return {"id": product_id, "cached": True}
    row = db.execute("SELECT * FROM products WHERE id=?", (product_id,))  # db query
    return {"id": product_id, "row": row.fetchone()}


@app.post("/checkout")
def checkout(order_id: int, amount: float):
    # external payment call — a downstream dependency that can fail
    resp = httpx.post("https://payments.example.com/charge",
                      json={"order": order_id, "amount": amount})
    return {"ok": resp.status_code == 200, "order": order_id}


@app.get("/health")
def health():
    return {"status": "ok"}
