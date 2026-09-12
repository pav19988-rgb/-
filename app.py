import re
import math
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import folium
import streamlit as st
from streamlit_folium import st_folium


# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================

MSK = timezone(timedelta(hours=3))
UTC = timezone.utc

URL_ORDERS = "https://yamagistrali.ru/api/orders/v0/transferOrder/getFlatForCustomer"
URL_FLIGHTS = "https://yamagistrali.ru/api/flights/v1/listForForwarder"
URL_EVENTS = "https://yamagistrali.ru/api/flights/events/v1/listForForwarder"

ORDER_LIMITS = (100, 500, 1000, 5000)
MAX_DAYS = 14
MAX_FLIGHTS = 1000
NAIVE_TIMEZONE = None
ORDER_BATCH_SIZE = 50
FLIGHT_RESPONSE_LIMIT = 5000

EVENTS = {
    "goToTheLoading": ("loading", "Направился к загрузке"),
    "arrivalLoading": ("loading", "Прибыл на загрузку"),
    "startLoading": ("loading", "Начало загрузки"),
    "endLoading": ("loading", "Окончание загрузки"),
    "departureLoading": ("loading", "Убыл с загрузки"),
    "arrivalUnloading": ("unloading", "Прибыл на выгрузку"),
    "startUnloading": ("unloading", "Начало выгрузки"),
    "endUnloading": ("unloading", "Окончание выгрузки"),
    "departureUnloading": ("unloading", "Убыл с выгрузки"),
}
FOUR = (
    ("arrival", "Прибыл"),
    ("start", "Начало"),
    ("end", "Окончание"),
    ("departure", "Убыл"),
)
KINDS = {"loading": "Загрузка", "unloading": "Выгрузка", "transit": "Транзит"}

STATUS_LABELS = {"notStarted": "Не начат", "onFlight": "В рейсе"}
SUBSTATUS_LABELS = {"waitForFlightStart": "Ожидает начала рейса"}

MOSCOW_WORDS = {
    "москва", "московская", "мособл", "подольск",
    "боброво", "электросталь", "пушкино", "мытищи",
    "химки", "домодедово", "чехов", "ногинск", "люберцы",
    "балашиха", "коледино", "гривно", "обухово",
    "реутов", "щелково", "одинцово",
}


# ============================================================
# УТИЛИТЫ (без изменений из Colab-версии)
# ============================================================

def txt(v):
    return "" if v is None else str(v).strip()


def html(v):
    return escape(txt(v), quote=True)


def norm(v):
    return " ".join(txt(v).casefold().replace("ё", "е").split())


def obj(v):
    return v if isinstance(v, dict) else {}


def objects(v, label="список"):
    if v is None:
        return []
    if not isinstance(v, list) or any(not isinstance(x, dict) for x in v):
        raise ValueError(f"Неверная структура: {label}")
    return v


def identifier(v):
    if v is None:
        return ""
    if isinstance(v, bool) or not isinstance(v, (str, int)):
        raise ValueError("Некорректный тип ID")
    return str(v).strip()


def raw_time(v):
    if isinstance(v, dict):
        v = v.get("time")
    return txt(v)


def parse_time(v):
    raw = raw_time(v)
    if not raw or ("T" not in raw and " " not in raw):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            if NAIVE_TIMEZONE is None:
                return None
            dt = dt.replace(tzinfo=NAIVE_TIMEZONE)
        return dt.astimezone(MSK)
    except (ValueError, TypeError, OverflowError):
        return None


def fmt(v, seconds=False):
    raw = raw_time(v)
    if not raw:
        return "—"
    dt = parse_time(v)
    if dt is None:
        return f"⚠️ {raw}"
    return dt.strftime("%d.%m.%Y %H:%M:%S" if seconds else "%d.%m.%Y %H:%M")


def period_text(a, b):
    if not raw_time(a) and not raw_time(b):
        return "План не указан"
    return f"{fmt(a)} → {fmt(b)}"


def number(v):
    if isinstance(v, bool):
        return None
    try:
        r = float(v)
        return r if math.isfinite(r) else None
    except (TypeError, ValueError):
        return None


def ordered(items, field):
    values = [number(i.get(field)) for i in items]
    valid = (
        all(v is not None for v in values)
        and len(set(values)) == len(values)
    )
    if valid:
        return sorted(items, key=lambda i: number(i[field])), True
    return list(items), False


def address(p):
    p = obj(p)
    geo = obj(p.get("npGeoAddress"))
    detailed = obj(geo.get("npAddress"))
    base = (
        txt(geo.get("address"))
        or txt(detailed.get("displayAddress"))
        or txt(detailed.get("address"))
        or txt(p.get("address"))
    )
    comment = txt(p.get("addressComment"))
    if re.fullmatch(r"[\d\s_-]+", comment or ""):
        comment = ""
    if base and comment and norm(comment) not in norm(base):
        return f"{comment} · {base}"
    return base or comment or txt(p.get("name")) or "Адрес не указан"


def region_text(p):
    p = obj(p)
    geo = obj(p.get("npGeoAddress"))
    detailed = obj(geo.get("npAddress"))
    return " ".join(
        txt(src.get(k))
        for src in (p, geo, detailed)
        for k in ("province", "locality", "address", "displayAddress")
        if src.get(k)
    )


def coords(p):
    p = obj(p)
    geo = obj(p.get("npGeoAddress"))
    detailed = obj(geo.get("npAddress"))
    for src in (geo, detailed, p):
        lat, lon = src.get("lat"), src.get("lon") or src.get("long")
        lat, lon = number(lat), number(lon)
        if (
            lat is not None and lon is not None
            and -90 <= lat <= 90 and -180 <= lon <= 180
        ):
            return lat, lon
    return None


def party(f, field):
    return txt(obj(f.get(field)).get("name")) or "Не указан"


def inactive(task, rel=None):
    rel = obj(rel)
    return any(v is True for v in (
        task.get("isDeleted"),
        task.get("isRevoked"),
        rel.get("isTransferTaskDeleted"),
        rel.get("isTransferTaskRevoked"),
    ))


def event_deleted(e):
    return bool(raw_time(e.get("deletedAt")))


def task_times(task, kind):
    if kind not in ("loading", "unloading"):
        return {}
    suffix = "Loading" if kind == "loading" else "Unloading"
    info = obj(task.get("flightInfo"))
    return {p: info.get(f"{p}{suffix}At") for p, _ in FOUR}


# ============================================================
# API
# ============================================================

class ApiError(RuntimeError):
    pass


class Api:
    def __init__(self, token):
        token = txt(token)
        if not token:
            raise ValueError("Ключ пустой")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        retry = Retry(
            total=3, backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"POST"}),
            respect_retry_after_header=True, raise_on_status=False,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def read(self, url, data, field):
        try:
            response = self.session.post(url, json={"data": data}, timeout=(10, 90))
        except requests.RequestException as exc:
            raise ApiError(f"Сетевая ошибка: {type(exc).__name__}") from None
        try:
            if response.status_code in (401, 403):
                raise ApiError(f"HTTP {response.status_code}: проверьте ключ.")
            if not 200 <= response.status_code < 300:
                raise ApiError(f"HTTP {response.status_code}")
            try:
                body = response.json()
            except ValueError:
                raise ApiError("Ответ не JSON") from None
            if not isinstance(body, dict):
                raise ApiError("Неожиданный формат ответа")
            if body.get("error") or body.get("errors"):
                raise ApiError("API вернул ошибку")
            payload = body.get("data")
            if not isinstance(payload, dict) or field not in payload:
                raise ApiError(f"Нет data.{field}")
            return objects(payload[field], f"data.{field}")
        finally:
            response.close()

    def orders(self, day):
        start = datetime(day.year, day.month, day.day, tzinfo=MSK)
        end = start + timedelta(days=1) - timedelta(milliseconds=1)

        def ts(dt):
            return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

        previous = set()
        for limit in ORDER_LIMITS:
            got = self.read(URL_ORDERS, {
                "filter": {
                    "fromLoadingTS": {"time": ts(start), "displayTZ": "+03:00"},
                    "toLoadingTS": {"time": ts(end), "displayTZ": "+03:00"},
                },
                "sort": {"sortSettings": []},
                "limit": limit,
            }, "orders")
            ids = [identifier(i.get("id")) for i in got]
            if any(not i for i in ids):
                raise ApiError("Заказ без ID")
            if not previous.issubset(set(ids)):
                raise ApiError("Выдача изменилась: повторите загрузку")
            if len(got) < limit:
                return got
            previous = set(ids)
        raise ApiError("Достигнут максимум заказов")

    def flight(self, flight_id):
        flight_id = identifier(flight_id)
        got = self.read(URL_FLIGHTS, {"filter": {"exactIds": [flight_id]}}, "flights")
        if len(got) != 1 or identifier(got[0].get("id")) != flight_id:
            raise ApiError("Точный запрос рейса не вернул результат")
        return got[0]

    def events(self, flight_id):
        return self.read(URL_EVENTS, {"flightId": identifier(flight_id)}, "flightTasks")

    def flights_by_order_ids(self, order_ids):
        flights = {}
        batches = (len(order_ids) + ORDER_BATCH_SIZE - 1) // ORDER_BATCH_SIZE
        for offset in range(0, len(order_ids), ORDER_BATCH_SIZE):
            batch = order_ids[offset:offset + ORDER_BATCH_SIZE]
            got = self.read(URL_FLIGHTS, {
                "filter": {"orderIds": batch},
                "limit": FLIGHT_RESPONSE_LIMIT,
            }, "flights")
            if len(got) >= FLIGHT_RESPONSE_LIMIT:
                raise ApiError("Достигнут лимит ответа списка рейсов")
            for f in got:
                fid = identifier(f.get("id"))
                if not fid:
                    raise ApiError("Рейс без ID")
                flights[fid] = f
        return flights


# ============================================================
# АНАЛИЗ РЕЙСА
# ============================================================

def build_model(flight):
    fid = identifier(flight.get("id"))
    tasks = {}
    warnings = []
    for task in objects(flight.get("tasks"), "tasks"):
        tid = identifier(task.get("flightTaskId"))
        if not tid or tid in tasks:
            raise ValueError("Плохой flightTaskId")
        tasks[tid] = task

    way = obj(flight.get("flightWay"))
    raw_visits = objects(way.get("visits"), "flightWay.visits")
    raw_visits, valid = ordered(raw_visits, "orderInWay")
    if raw_visits and not valid:
        warnings.append("Порядок посещений неоднозначен.")

    visits = []
    covered = set()
    seen = set()
    bindings = defaultdict(list)

    for idx, visit in enumerate(raw_visits):
        vid = identifier(visit.get("id"))
        if not vid or vid in seen:
            raise ValueError("Плохой visit.id")
        seen.add(vid)

        point = obj(visit.get("wayPoint"))
        actions, _ = ordered(objects(visit.get("actions"), "actions"), "orderInVisit")

        operations = []
        for action in actions:
            rel = obj(action.get("relation"))
            tid = identifier(rel.get("flightTaskId"))
            task = tasks.get(tid)
            kind = txt(action.get("actionType"))
            rel_fid = identifier(rel.get("flightId"))

            if rel_fid and rel_fid != fid:
                task = None

            if task is not None and kind in ("loading", "unloading"):
                covered.add((tid, kind))

            op = {
                "task_id": tid,
                "kind": kind,
                "task": task,
                "relation": rel,
                "inactive": inactive(task or {}, rel),
                "times": task_times(task or {}, kind),
                "order_id": identifier(
                    rel.get("orderId")
                    if rel.get("orderId") is not None
                    else obj((task or {}).get("orderInfo")).get("orderId")
                ),
                "shipment_id": identifier((task or {}).get("shipmentId")),
                "relation_shipment_id": identifier(rel.get("shipmentId")),
            }
            operations.append(op)

            if task is not None and kind in ("loading", "unloading"):
                bindings[(tid, kind)].append((vid, address(point)))

        visits.append({
            "id": vid,
            "order": visit.get("orderInWay"),
            "point": point,
            "address": address(point),
            "start": visit.get("startAt"),
            "end": visit.get("endAt"),
            "operations": operations,
            "fallback": False,
        })

    fallback = []
    for tid, task in tasks.items():
        for kind, pk, _ in (
            ("loading", "npShipment", None),
            ("unloading", "npUnshipment", None),
        ):
            p = obj(task.get(pk))
            if not p or (tid, kind) in covered:
                continue
            period = obj(p.get("period"))
            fallback.append({
                "id": f"fallback:{tid}:{kind}",
                "order": None,
                "point": p,
                "address": address(p),
                "start": period.get("from"),
                "end": period.get("to"),
                "operations": [{
                    "task_id": tid, "kind": kind, "task": task,
                    "relation": {}, "inactive": inactive(task),
                    "times": task_times(task, kind),
                    "order_id": identifier(obj(task.get("orderInfo")).get("orderId")),
                    "shipment_id": identifier(task.get("shipmentId")),
                    "relation_shipment_id": "",
                }],
                "fallback": True,
            })
    if fallback:
        visits.extend(fallback)

    for v in visits:
        for op in v["operations"]:
            op["ambiguous"] = len(bindings.get((op["task_id"], op["kind"]), [])) > 1

    return {
        "flight": flight,
        "tasks": tasks,
        "visits": visits,
        "bindings": bindings,
        "warnings": list(dict.fromkeys(warnings)),
    }


def visible_visits(model, show_inactive):
    out = []
    for v in model["visits"]:
        ops = [op for op in v["operations"] if show_inactive or not op["inactive"]]
        if ops or not v["operations"]:
            out.append({**v, "operations": ops})
    return out


def flatten_events(tasks):
    out = []
    for task in objects(tasks, "flightTasks"):
        tid = identifier(task.get("id"))
        for group in objects(task.get("events"), "groups"):
            for e in objects(group.get("events"), "events"):
                out.append({
                    "task_id": tid,
                    "id": identifier(e.get("id")),
                    "role": txt(group.get("role")),
                    "eventType": txt(e.get("eventType")),
                    "actualDate": e.get("actualDate"),
                    "deletedAt": e.get("deletedAt"),
                })
    out.sort(key=lambda e: (
        parse_time(e["actualDate"]) is None,
        parse_time(e["actualDate"]) or datetime.max.replace(tzinfo=MSK),
    ))
    return out


def latest_history(model, events):
    facts = []
    for e in events:
        if event_deleted(e):
            continue
        t = model["tasks"].get(e["task_id"])
        if t is None or inactive(t):
            continue
        dt = parse_time(e["actualDate"])
        if dt is None:
            continue
        d = EVENTS.get(e["eventType"])
        label = d[1] if d else e["eventType"]
        facts.append((dt, label))
    if not facts:
        return "Нет применимых событий."
    latest = max(f[0] for f in facts)
    labels = sorted({l for dt, l in facts if dt == latest})
    return f"{latest:%d.%m.%Y %H:%M:%S} МСК — " + " | ".join(labels)


def latest_snapshot(model):
    facts = []
    for tid, task in model["tasks"].items():
        if inactive(task):
            continue
        info = obj(task.get("flightInfo"))
        for et, (kind, label) in EVENTS.items():
            dt = parse_time(info.get(et + "At"))
            if dt is not None:
                p = obj(task.get("npShipment" if kind == "loading" else "npUnshipment"))
                facts.append((dt, label, address(p)))
    if not facts:
        return "Фактические времена не указаны."
    latest = max(f[0] for f in facts)
    labels = sorted({f"{l} · {w}" for dt, l, w in facts if dt == latest})
    return f"{latest:%d.%m.%Y %H:%M:%S} МСК — " + " | ".join(labels)


# ============================================================
# HTML-РЕНДЕР КАРТОЧКИ
# ============================================================

def visit_block(visit, position, total):
    ops = visit["operations"]
    kinds = list(dict.fromkeys(
        KINDS.get(op["kind"], op["kind"] or "—") for op in ops
    ))
    label = " / ".join(kinds) or "Посещение"

    rows = []
    relevant = [op for op in ops if op["kind"] in ("loading", "unloading")]
    for field, name in FOUR:
        values = [raw_time(op["times"].get(field)) for op in relevant if raw_time(op["times"].get(field))]
        uniq = list(dict.fromkeys(values))
        val = "—" if not uniq else (fmt(uniq[0], seconds=True) if len(uniq) == 1 else f"различаются ({len(uniq)})")
        cov = f"{len(values)}/{len(relevant)}" if relevant else "—"
        rows.append(f"<tr><td>{name}</td><td>{html(val)}</td><td>{cov}</td></tr>")

    cargo_html = cargo_block(visit)

    return (
        f"<div class='mg-stop'>"
        f"<b>{position}/{total} · {html(label)}</b><br>"
        f"<span class='mg-addr'>{html(visit['address'])}</span><br>"
        f"<small>{html(period_text(visit['start'], visit['end']))}</small>"
        f"<table class='mg-t'>{''.join(rows)}</table>"
        f"{cargo_html}"
        "</div>"
    )


def cargo_block(visit):
    ops = visit.get("operations") or []
    if not ops:
        return "<div class='mg-muted'>Грузовые операции не указаны.</div>"

    parts = []
    seen = set()
    for op in ops:
        task = obj(op.get("task"))
        tid = identifier(op.get("task_id"))
        kind = txt(op.get("kind"))
        key = (tid, kind)
        if tid and key in seen:
            continue
        if tid:
            seen.add(key)

        order_info = obj(task.get("orderInfo"))
        oid = (
            identifier(op.get("order_id"))
            or identifier(order_info.get("orderId"))
        )
        order_name = txt(order_info.get("transferOrderName"))

        src = address(obj(task.get("npShipment"))) if task.get("npShipment") else "—"
        dst = address(obj(task.get("npUnshipment"))) if task.get("npUnshipment") else "—"

        if kind == "unloading":
            action = "📦 Выгружается здесь"; color = "#b45309"
        elif kind == "loading":
            action = "🚛 Загружается здесь"; color = "#166534"
        elif kind == "transit":
            action = "↪ Транзит"; color = "#475569"
        else:
            action = KINDS.get(kind, kind or "—"); color = "#475569"

        pkg_rows = []
        for pkg in objects(task.get("packages"), "packages"):
            if pkg.get("isDeleted") is True:
                continue
            pkg_rows.append(
                "<tr>"
                f"<td>{html(txt(pkg.get('packageName')) or '—')}</td>"
                f"<td>{html(pkg.get('count', '—'))}</td>"
                f"<td>{html(pkg.get('weight', '—'))}</td>"
                f"<td>{html(pkg.get('capacity', '—'))}</td>"
                "</tr>"
            )
        pkg_html = (
            "<table class='mg-t'><tr><th>Груз</th><th>Кол-во</th><th>Вес</th><th>Cap.</th></tr>"
            + "".join(pkg_rows) + "</table>"
            if pkg_rows else
            "<div class='mg-muted'>Состав груза не указан.</div>"
        )

        title = f"Заказ {oid or 'без ID'}"
        if order_name:
            title += f" · {order_name}"

        parts.append(
            f"<div class='mg-cargo'>"
            f"<div style='color:{color};font-weight:600'>{html(action)}</div>"
            f"<b>{html(title)}</b>"
            f"<div class='mg-muted'>Со склада: {html(src)}<br>Куда: {html(dst)}</div>"
            f"<div class='mg-muted'>Задача: {html(tid or '—')}</div>"
            f"{pkg_html}</div>"
        )

    return (
        f"<details class='mg-cargo-wrap'>"
        f"<summary>📦 Грузы и заказы · {len(parts)}</summary>"
        + "".join(parts) +
        "</details>"
    )


def map_view(visits, key):
    points = [(i, v, coords(v["point"])) for i, v in enumerate(visits, 1) if coords(v["point"])]
    if not points:
        st.warning("Корректных координат нет.")
        return
    m = folium.Map(location=points[0][2], zoom_start=11, tiles="OpenStreetMap")
    grouped = defaultdict(list)
    for i, v, c in points:
        grouped[c].append((i, v))
    for c, group in grouped.items():
        parts = []
        for i, v in group:
            kinds = ", ".join(sorted({KINDS.get(op["kind"], op["kind"]) for op in v["operations"]})) or "—"
            parts.append(
                f"<b>Посещение {i} · {html(kinds)}</b><br>"
                f"{html(v['address'])}<br>{html(period_text(v['start'], v['end']))}"
            )
        popup = folium.Popup(folium.IFrame("<hr>".join(parts), width=350, height=200), max_width=380)
        folium.Marker(c, popup=popup, icon=folium.Icon(color="blue", icon="info-sign")).add_to(m)
    if len(grouped) > 1:
        m.fit_bounds(list(grouped.keys()), padding=(30, 30), max_zoom=14)
    st_folium(m, width=None, height=500, key=key)


# ============================================================
# СБОРКА ФИЛЬТРОВ
# ============================================================

def build_filters(models, current_orders):
    catalogs = {k: {} for k in (
        "region", "carrier", "driver", "truck_filter",
        "customer_filter", "flight_filter", "point",
    )}
    rows = []

    def register(field, entity, label):
        entity = obj(entity)
        val = identifier(entity.get("id"))
        key = "id:" + val if val else ("name:" + norm(label) if txt(label) else None)
        if key:
            catalogs[field].setdefault(key, txt(label) or f"ID {val}")
        return key

    def point_key(p):
        p = obj(p)
        val = identifier(p.get("id"))
        if val:
            return "id:" + val
        lbl = address(p)
        return "address:" + norm(lbl) if lbl and lbl != "Адрес не указан" else None

    def point_region(p):
        p = obj(p)
        geo = obj(p.get("npGeoAddress"))
        detailed = obj(geo.get("npAddress"))
        return txt(geo.get("province")) or txt(detailed.get("province")) or txt(p.get("province"))

    by_order = defaultdict(list)
    by_task = defaultdict(list)
    for fid, m in models.items():
        f = m["flight"]
        nm = txt(f.get("flightName"))
        catalogs["flight_filter"]["id:" + fid] = f"Рейс {fid}" + (f" · {nm}" if nm else "")
        for tid, task in m["tasks"].items():
            if inactive(task):
                continue
            oid = identifier(obj(task.get("orderInfo")).get("orderId"))
            if oid:
                by_order[oid].append((fid, task))
            by_task[tid].append((fid, task))

    used = set()

    def add_row(order, shipment, fid=None, task=None):
        order = obj(order); shipment = obj(shipment); task = obj(task)
        flight = models[fid]["flight"] if fid in models else {}
        oi = obj(task.get("orderInfo"))
        si = obj(shipment.get("flightInfo"))
        ti = obj(task.get("flightInfo"))
        customer = obj(order.get("customer")) or obj(oi.get("customer"))
        executor = obj(order.get("executor")) or obj(flight.get("executer"))
        driver = obj(si.get("driverInfo")) or obj(ti.get("driverInfo")) or obj(flight.get("driver"))
        truck = obj(si.get("carHeadInfo")) or obj(ti.get("carHeadInfo")) or obj(flight.get("truck"))
        truck_label = " · ".join(filter(None, [txt(truck.get("number")), txt(truck.get("brand") or truck.get("model"))]))
        points = []
        for field in ("npShipment", "npUnshipment"):
            p = obj(shipment.get(field)) or obj(task.get(field))
            if not p:
                continue
            pk = point_key(p)
            if pk:
                catalogs["point"].setdefault(pk, address(p))
            reg = point_region(p)
            rk = "region:" + norm(reg) if reg else None
            if rk:
                catalogs["region"].setdefault(rk, reg)
            points.append({"point": pk, "region": rk})
        rows.append({
            "fid": fid,
            "customer_filter": register("customer_filter", customer, customer.get("customerName") or customer.get("name")),
            "carrier": register("carrier", executor, executor.get("name")),
            "driver": register("driver", driver, driver.get("name")),
            "truck_filter": register("truck_filter", truck, truck_label),
            "flight_filter": "id:" + fid if fid else None,
            "points": points,
        })

    for oid, order in current_orders.items():
        c = obj(order.get("customer"))
        e = obj(order.get("executor"))
        register("customer_filter", c, c.get("customerName") or c.get("name"))
        register("carrier", e, e.get("name"))
        for shipment in objects(order.get("shipments"), "shipments"):
            if shipment.get("isDeleted") is True:
                continue
            info = obj(shipment.get("flightInfo"))
            tid = identifier(shipment.get("flightTaskId") or info.get("flightTaskId"))
            matches = by_task.get(tid, []) if tid else []
            if matches:
                for fid, task in matches:
                    add_row(order, shipment, fid, task)
                    used.add((fid, identifier(task.get("flightTaskId"))))
            else:
                add_row(order, shipment)

    for oid, links in by_order.items():
        if oid not in current_orders:
            continue
        for fid, task in links:
            key = (fid, identifier(task.get("flightTaskId")))
            if key not in used:
                add_row(current_orders[oid], {}, fid, task)
                used.add(key)

    return catalogs, rows


# ============================================================
# STREAMLIT UI
# ============================================================

st.set_page_config(page_title="Мониторинг рейсов", layout="wide")

st.markdown("""
<style>
.mg-stop {
  padding: 12px; margin: 8px 0; background:#fafcfe;
  border:1px solid #dce5f0; border-left:4px solid #4a89c7;
  border-radius: 10px;
}
.mg-addr { color:#234f83; font-weight:600; }
.mg-cargo {
  padding: 10px; margin: 8px 0; background:#fff;
  border:1px solid #dce6f1; border-radius:8px;
}
.mg-cargo-wrap { margin-top: 10px; }
.mg-t { width:100%; border-collapse:collapse; font-size:13px; margin-top:6px; }
.mg-t th, .mg-t td { text-align:left; padding:5px 7px; border-bottom:1px solid #e2eaf3; }
.mg-t th { background:#edf3fa; }
.mg-muted { font-size:12px; color:#718197; }
</style>
""", unsafe_allow_html=True)

if "models" not in st.session_state:
    st.session_state.models = {}
    st.session_state.current_orders = {}
    st.session_state.event_cache = {}
    st.session_state.single_updates = {}
    st.session_state.snapshot_range = None
    st.session_state.unassigned_count = 0
    st.session_state.loaded = False

st.title("🚚 Мониторинг рейсов")

with st.sidebar:
    st.header("Подключение")
    token = st.text_input("API-ключ", type="password", value=st.session_state.get("token", ""))
    st.session_state.token = token

    st.header("Загрузка")
    today = datetime.now(MSK).date()
    c1, c2 = st.columns(2)
    start = c1.date_input("С", today)
    end = c2.date_input("По", today)

    if st.button("Загрузить рейсы", type="primary", use_container_width=True):
        if not token:
            st.error("Введите ключ.")
        elif not start or not end or start > end:
            st.error("Проверьте даты.")
        elif (end - start).days + 1 > MAX_DAYS:
            st.error(f"Максимум {MAX_DAYS} дней.")
        else:
            with st.spinner("Загружаем заказы и рейсы…"):
                try:
                    api = Api(token)
                    orders = {}
                    for offset in range((end - start).days + 1):
                        day = start + timedelta(days=offset)
                        for o in api.orders(day):
                            orders[identifier(o.get("id"))] = o
                    order_ids = [oid for oid in orders if oid]
                    flights = api.flights_by_order_ids(order_ids)
                    models = {fid: build_model(f) for fid, f in flights.items()}

                    st.session_state.models = models
                    st.session_state.current_orders = orders
                    st.session_state.event_cache = {}
                    st.session_state.single_updates = {}
                    st.session_state.snapshot_range = (start, end)
                    st.session_state.unassigned_count = sum(
                        1 for o in orders.values()
                        for s in objects(o.get("shipments"), "shipments")
                        if s.get("isDeleted") is not True
                        and not identifier(obj(s.get("flightInfo")).get("flightId"))
                    )
                    st.session_state.loaded = True
                    st.success(f"Заказов: {len(orders)} · рейсов: {len(models)}")
                except Exception as exc:
                    st.error(f"Ошибка: {exc}")

st.divider()

if not st.session_state.loaded:
    st.info("Выберите даты в боковой панели и нажмите «Загрузить рейсы».")
    st.stop()

# Метрики
col1, col2, col3, col4 = st.columns(4)
col1.metric("Заказов", len(st.session_state.current_orders))
col2.metric("Рейсов", len(st.session_state.models))
col3.metric("Отгрузок без flightId", st.session_state.unassigned_count)
col4.metric("Без координат", sum(1 for m in st.session_state.models.values() if not any(coords(v["point"]) for v in m["visits"])))

if st.session_state.snapshot_range:
    s, e = st.session_state.snapshot_range
    st.caption(f"Снимок: {s:%d.%m.%Y} — {e:%d.%m.%Y}")

# Фильтры
catalogs, rows = build_filters(st.session_state.models, st.session_state.current_orders)

st.subheader("Фильтры")
f1, f2, f3 = st.columns(3)

def _opts(field):
    catalog = catalogs[field]
    return [("Все", None)] + sorted([(lbl, key) for key, lbl in catalog.items()], key=lambda x: norm(x[0]))

f_point = f1.selectbox("🏪 Лавка / точка", _opts("point"), format_func=lambda x: x[0], key="f_point")
f_driver = f2.selectbox("👤 Водитель", _opts("driver"), format_func=lambda x: x[0], key="f_driver")
f_carrier = f3.selectbox("🚛 Перевозчик", _opts("carrier"), format_func=lambda x: x[0], key="f_carrier")

f4, f5, f6 = st.columns(3)
f_truck = f4.selectbox("🚚 Автомобиль", _opts("truck_filter"), format_func=lambda x: x[0], key="f_truck")
f_customer = f5.selectbox("📦 Заказчик", _opts("customer_filter"), format_func=lambda x: x[0], key="f_customer")
f_region = f6.selectbox("📍 Регион", _opts("region"), format_func=lambda x: x[0], key="f_region")

f7, f8 = st.columns(2)
f_flight = f7.selectbox("🛣 Рейс", _opts("flight_filter"), format_func=lambda x: x[0], key="f_flight")
only_active = f8.checkbox("Только «В рейсе»")

show_inactive = st.checkbox("Показывать отозванные / удалённые")

# Матчинг
selected = {
    "point": f_point[1], "driver": f_driver[1], "carrier": f_carrier[1],
    "truck_filter": f_truck[1], "customer_filter": f_customer[1],
    "region": f_region[1], "flight_filter": f_flight[1],
}

if all(v is None for v in selected.values()):
    matching = set(st.session_state.models)
else:
    matching = set()
    for row in rows:
        if any(selected[f] is not None and row.get(f) != selected[f] for f in (
            "carrier", "driver", "truck_filter", "customer_filter", "flight_filter",
        )):
            continue
        if selected["point"] is not None or selected["region"] is not None:
            ok = any(
                (selected["point"] is None or p.get("point") == selected["point"])
                and (selected["region"] is None or p.get("region") == selected["region"])
                for p in row.get("points", [])
            )
            if not ok:
                continue
        if row.get("fid") in st.session_state.models:
            matching.add(row["fid"])

if only_active:
    matching = {fid for fid in matching if st.session_state.models[fid]["flight"].get("status") == "onFlight"}

matching = sorted(
    matching,
    key=lambda fid: (
        st.session_state.models[fid]["flight"].get("status") != "onFlight",
        fid,
    ),
)

st.divider()
st.subheader(f"Найдено рейсов: {len(matching)}")

PAGE_SIZE = 5
pages = max(1, math.ceil(len(matching) / PAGE_SIZE))
page = st.number_input("Страница", 1, pages, 1)
current_ids = matching[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

for fid in current_ids:
    model = st.session_state.models[fid]
    flight = model["flight"]
    visits = visible_visits(model, show_inactive)

    status = STATUS_LABELS.get(txt(flight.get("status")), txt(flight.get("status")) or "—")
    header = f"🚚 Рейс {fid} · {status} · {party(flight, 'driver')} · {party(flight, 'executer')}"

    with st.expander(header):
        st.caption(f"Последний датированный факт: {latest_snapshot(model)}")

        c1, c2 = st.columns([1, 1])
        if c1.button("🔄 Обновить этот рейс", key=f"ref_{fid}"):
            with st.spinner("Обновляем рейс и события…"):
                try:
                    api = Api(st.session_state.token)
                    fresh = api.flight(fid)
                    fresh_model = build_model(fresh)
                    fresh_events = flatten_events(api.events(fid))
                    st.session_state.models[fid] = fresh_model
                    st.session_state.event_cache[fid] = (datetime.now(MSK), fresh_events)
                    st.session_state.single_updates[fid] = (datetime.now(MSK), fresh_events)
                    st.success("Обновлено")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Ошибка: {exc}")

        upd = st.session_state.single_updates.get(fid)
        if upd:
            at, evs = upd
            st.info(f"Рейс обновлён {at:%d.%m.%Y %H:%M:%S} МСК. {latest_history(st.session_state.models[fid], evs)}")

        for i, v in enumerate(visits, 1):
            st.markdown(visit_block(v, i, len(visits)), unsafe_allow_html=True)

        c1, c2 = st.columns(2)
        if c1.button("🗺 Показать карту", key=f"map_{fid}"):
            st.session_state[f"show_map_{fid}"] = not st.session_state.get(f"show_map_{fid}", False)
        if c2.button("🕒 Загрузить хронологию", key=f"hist_{fid}"):
            with st.spinner("Запрашиваем события…"):
                try:
                    api = Api(st.session_state.token)
                    events = flatten_events(api.events(fid))
                    st.session_state.event_cache[fid] = (datetime.now(MSK), events)
                except Exception as exc:
                    st.error(f"Ошибка: {exc}")

        if st.session_state.get(f"show_map_{fid}"):
            map_view(visits, key=f"folium_{fid}")

        cached = st.session_state.event_cache.get(fid)
        if cached:
            at, events = cached
            st.caption(f"Хронология загружена {at:%H:%M:%S}")
            rows_h = []
            for e in events:
                task = st.session_state.models[fid]["tasks"].get(e["task_id"])
                if not show_inactive and (event_deleted(e) or (task and inactive(task))):
                    continue
                d = EVENTS.get(e["eventType"])
                label = d[1] if d else e["eventType"]
                rows_h.append(
                    f"<tr><td>{html(fmt(e['actualDate'], seconds=True))}</td>"
                    f"<td>{html(label)}</td>"
                    f"<td>{html(e['role'] or '—')}</td></tr>"
                )
            if rows_h:
                st.markdown(
                    "<table class='mg-t'><tr><th>МСК</th><th>Событие</th><th>Роль</th></tr>"
                    + "".join(rows_h) + "</table>",
                    unsafe_allow_html=True,
                )