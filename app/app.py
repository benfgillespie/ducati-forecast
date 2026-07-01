import io
import json
import os
import re
import secrets
from datetime import date
from functools import wraps

from flask import (Flask, request, redirect, url_for, render_template,
                   session, flash, jsonify)

from . import db
from . import parsers
from . import seed_data

app = Flask(__name__)
app.teardown_appcontext(db.close_conn)


class _PrefixMiddleware:
    """Strip a URL prefix from incoming PATH_INFO so Flask routes match their
    decorator paths, while keeping url_for output prefixed via SCRIPT_NAME."""

    def __init__(self, wsgi_app, prefix):
        self.wsgi_app = wsgi_app
        self.prefix = prefix.rstrip("/")

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if self.prefix and path.startswith(self.prefix):
            environ["PATH_INFO"] = path[len(self.prefix):] or "/"
            environ["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "") + self.prefix
        return self.wsgi_app(environ, start_response)


_url_prefix = os.environ.get("URL_PREFIX", "").rstrip("/")
if _url_prefix:
    app.wsgi_app = _PrefixMiddleware(app.wsgi_app, _url_prefix)


def _bootstrap():
    """Run once on startup: init DB, seed map, ensure secret key + admin password."""
    db.init_db()
    raw = db._connect()
    cur = raw.cursor()
    cur.execute(db.q("SELECT value FROM settings WHERE key=?"), ("secret_key",))
    row = cur.fetchone()
    if row is None:
        sk = secrets.token_hex(32)
        cur.execute(db.q("INSERT INTO settings(key,value) VALUES(?,?)"),
                    ("secret_key", sk))
    else:
        sk = row["value"]
    cur.execute(db.q("SELECT value FROM settings WHERE key=?"), ("admin_password",))
    row = cur.fetchone()
    if row is None:
        ap = os.environ.get("ADMIN_PASSWORD", "admin")
        cur.execute(db.q("INSERT INTO settings(key,value) VALUES(?,?)"),
                    ("admin_password", ap))
        if ap == "admin":
            print(f"[bootstrap] default admin password set to: 'admin' — change it via /admin/dealers")

    # seed material map via a tiny shim that uses q() too
    class _SeedShim:
        def execute(self, sql, params=()):
            cur.execute(db.q(sql), params)
            return cur
        def commit(self):
            raw.commit()
    seed_data.seed(_SeedShim())
    raw.commit()
    raw.close()
    app.secret_key = sk


_boot_error_message = None
try:
    _bootstrap()
except Exception as _e:
    _boot_error_message = f"{type(_e).__name__}: {_e}"
    print(f"[bootstrap] deferred startup error: {_boot_error_message}")
    app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def _bootstrap_error(path):
        msg = _boot_error_message
        return (
            "<h1>Dashboard not configured yet</h1>"
            "<p>The Flask app started but couldn't initialise its database. "
            "On Vercel, add a Postgres database (Storage → Create → Postgres) "
            f"so <code>DATABASE_URL</code> is set, then redeploy.</p><pre>{msg}</pre>",
            503,
            {"Content-Type": "text/html; charset=utf-8"},
        )


# ----- auth helpers ---------------------------------------------------

def current_dealer():
    pw = session.get("dealer_pw")
    if not pw:
        return None
    return db.get_conn().execute(
        "SELECT * FROM dealers WHERE password=?", (pw,)
    ).fetchone()


def is_admin():
    return session.get("admin") is True


def dealer_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not current_dealer():
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not is_admin():
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrapper


# ----- login / logout -------------------------------------------------

@app.route("/login", methods=["GET"])
def login():
    return render_template("login.html")


@app.route("/login", methods=["POST"])
def login_post():
    pw = request.form.get("password", "").strip()
    if not pw:
        flash("Enter a password.", "error")
        return redirect(url_for("login"))
    admin_pw = db.get_setting("admin_password")
    if pw == admin_pw:
        session.clear()
        session["admin"] = True
        return redirect(url_for("admin_home"))
    dealer = db.get_conn().execute(
        "SELECT * FROM dealers WHERE password=?", (pw,)
    ).fetchone()
    if dealer:
        session.clear()
        session["dealer_pw"] = pw
        return redirect(url_for("dealer_view"))
    flash("Unknown password.", "error")
    return redirect(url_for("login"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


# ----- feedback widget ------------------------------------------------

@app.route("/feedback", methods=["POST"])
def submit_feedback():
    message = (request.form.get("message") or "").strip()
    if not message:
        return jsonify(ok=False, error="empty"), 400
    if len(message) > 5000:
        message = message[:5000]
    page = (request.form.get("page") or "").strip()[:500] or None
    if is_admin():
        submitter = "admin"
    else:
        dealer = current_dealer()
        submitter = f"dealer:{dealer['name']}" if dealer else "anonymous"
    con = db.get_conn()
    con.execute(
        "INSERT INTO feedback(submitter, page, message) VALUES(?,?,?)",
        (submitter, page, message),
    )
    con.commit()
    return jsonify(ok=True)


# ----- dealer view ----------------------------------------------------

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _rolling_months(start=None, n=12):
    """Return list of (year, month) tuples for the next n months, starting from this month."""
    today = start or date.today()
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append((y, m))
        m += 1
        if m == 13:
            m = 1
            y += 1
    return out


def _dealer_months_visible():
    """How many month columns dealers see (admin-configurable, 1–12).
    The forecast math + 'Soonest' calculation always use the full 12 months;
    this only affects column rendering on the dealer view."""
    try:
        n = int(db.get_setting("dealer_months_visible") or 12)
    except (TypeError, ValueError):
        n = 12
    return max(1, min(12, n))


def _auto_accounting_month():
    """(date-on-the-1st, source) inferred from the loaded allocations.

    The allocation file's sheets/filename are dated (e.g. '12.06.26'), and those
    dates are stored on each allocation_reports row. The month those bikes were
    allocated in IS the accounting month — we don't have to guess it from the
    wall clock. Weighted by row_count so a stray off-month sheet can't outvote
    the real period; ties break to the earliest month. Falls back to today's
    month (source 'default') when no allocations are loaded."""
    weights = {}
    for r in db.get_conn().execute(
        "SELECT report_date, row_count FROM allocation_reports "
        "WHERE report_date IS NOT NULL AND report_date <> ''"
    ).fetchall():
        ym = str(r["report_date"])[:7]           # 'YYYY-MM' from 'YYYY-MM-DD am|pm'
        if len(ym) == 7 and ym[4] == "-":
            weights[ym] = weights.get(ym, 0) + (r["row_count"] or 0) + 1
    if weights:
        top = max(weights.values())
        best = min(k for k, v in weights.items() if v == top)  # most bikes; ties → earliest
        try:
            y, m = best.split("-")
            return date(int(y), int(m), 1), "auto"
        except ValueError:
            pass
    return date.today().replace(day=1), "default"


def _accounting_anchor():
    """(date-on-the-1st, source) the 12-month availability window starts from.

    This is the *accounting month* — the month the loaded figures belong to —
    NOT simply today's calendar month. The whole grid (which plan months load,
    the column headers, and the front of the queue that allocations are poured
    into) hangs off this. Anchoring on the wall clock shifts availability by a
    month the instant the calendar rolls over while the data still reflects the
    prior period. Resolution: an explicit admin override ('manual') wins; else
    auto-detect from the allocation dates ('auto' / 'default')."""
    override = (db.get_setting("accounting_month") or "").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})$", override)
    if m and 1 <= int(m.group(2)) <= 12:
        return date(int(m.group(1)), int(m.group(2)), 1), "manual"
    return _auto_accounting_month()


def _accounting_month_state():
    """Display bundle for the admin UI: current anchor, its source, the value
    for the <input type=month>, and what auto-detect alone would pick (so the
    admin can see what a manual pin is overriding)."""
    anchor, source = _accounting_anchor()
    auto_date, _ = _auto_accounting_month()
    return {
        "value": f"{anchor.year:04d}-{anchor.month:02d}",
        "label": f"{MONTH_NAMES[anchor.month - 1]} {anchor.year}",
        "source": source,
        "auto_label": f"{MONTH_NAMES[auto_date.month - 1]} {auto_date.year}",
    }


# Synthetic "general dealer": no dealer_code, so no per-dealer orders are shown —
# just the country-wide availability grid that every dealer sees.
GENERAL_DEALER = {"name": "Bike availability", "country": "UK", "dealer_code": None}


@app.route("/", methods=["GET"])
def home():
    """Public landing — the general dealer availability view, no login required.
    Logged-in dealers get their own view (with committed orders) at /dealer."""
    return _render_dealer(dict(GENERAL_DEALER), is_admin_view=False)


@app.route("/dealer")
@dealer_required
def dealer_view():
    return _render_dealer(current_dealer(), is_admin_view=False)


@app.route("/admin/forecast")
@admin_required
def admin_forecast():
    # admin's full analytical view: all 12 months + per-row story drill-down
    fake = {"name": "Admin forecast", "country": "UK", "dealer_code": None}
    return _render_dealer(fake, is_admin_view=True)


@app.route("/admin/dealer-view")
@admin_required
def admin_dealer_view():
    # mirrors exactly what a dealer sees (truncated months, no story panel)
    fake = {"name": "Dealer view (admin)", "country": "UK", "dealer_code": None}
    return _render_dealer(fake, is_admin_view=False)


def _render_dealer(dealer, is_admin_view=False):
    con = db.get_conn()

    anchor_date, anchor_source = _accounting_anchor()
    months = _rolling_months(start=anchor_date, n=12)
    months_keys = [(y, m) for (y, m) in months]
    # Dealers see only the first N months; admin preview always sees all 12.
    visible_n = 12 if is_admin_view else _dealer_months_visible()
    visible_months = months[:visible_n]

    # DUK-wide forecast: read the DUK_26 / DUK_27 rollup sheets directly (Ross sometimes
    # adjusts these manually so they don't equal the UK + SWE + NOR sum).
    placeholders = ",".join(["(?,?)"] * len(months_keys))
    flat = [v for ym in months_keys for v in ym]
    plan_rows = con.execute(
        f"SELECT plan_super, plan_model, year, month, qty "
        f"FROM plan "
        f"WHERE country='DUK' AND (year,month) IN ({placeholders}) "
        f"ORDER BY plan_super, plan_model, year, month",
        flat,
    ).fetchall()

    # All open Demo / Courtesy / End-customer orders across UK, SWE, NOR.
    # Sorted by order creation date so the FIFO allocation below is deterministic.
    order_rows = con.execute(
        "SELECT o.order_number, o.material_prefix, o.bike_type, o.end_customer_status, "
        "       o.order_creation_date, mm.plan_super, mm.plan_model "
        "FROM orders o "
        "LEFT JOIN material_map mm ON mm.material_prefix = o.material_prefix "
        "WHERE o.country IN ('UK','SWE','NOR') "
        "  AND ( o.bike_type IN ('Demo','Courtesy') OR o.end_customer_status='Yes' ) "
        "ORDER BY COALESCE(o.order_creation_date, '9999-12-31'), o.material_prefix"
    ).fetchall()

    # A bike's lifecycle is: forecast -> open order -> allocated. Once allocated it
    # should drop off the orders sheet and appear on the allocations sheet — it's
    # the SAME bike, so allocated + open-orders are meant to be disjoint. When the
    # two feeds are uploaded at slightly different times an order can briefly show
    # in both; counting it as allocated AND committed would subtract it twice. Treat
    # the allocation as the source of truth and skip any order already allocated.
    allocated_order_numbers = {
        r["order_number"] for r in con.execute(
            "SELECT DISTINCT order_number FROM allocations "
            "WHERE order_number IS NOT NULL AND order_number <> ''"
        ).fetchall()
    }

    # Allocations: bikes consumed from forecast capacity, off the FRONT of the queue.
    # EVERY allocation row counts — we deliberately do NOT filter by country. The
    # allocation sheet's country column is unreliable (some rows carry a stray code
    # like a dealer/customer number instead of a country), and per Ross anything on
    # the allocation sheet is a real allocation that should be counted. The breakdown
    # still buckets UK/SWE/NOR for display and lumps everything else under 'Other'.
    allocation_rows_by_country = con.execute(
        "SELECT mm.plan_model, a.country, COUNT(a.id) AS n "
        "FROM allocations a "
        "JOIN material_map mm ON mm.material_prefix = a.material_prefix "
        "WHERE mm.plan_model IS NOT NULL "
        "GROUP BY mm.plan_model, a.country"
    ).fetchall()
    alloc_total = {}
    alloc_by_country = {}   # plan_model -> {country: n}; non-UK/SWE/NOR -> 'Other'
    for r in allocation_rows_by_country:
        pm = r["plan_model"]
        alloc_total[pm] = alloc_total.get(pm, 0) + r["n"]
        bucket = r["country"] if r["country"] in ("UK", "SWE", "NOR") else "Other"
        by = alloc_by_country.setdefault(pm, {})
        by[bucket] = by.get(bucket, 0) + r["n"]

    # Allocations whose material prefix isn't mapped to a plan model are excluded
    # from the figures above entirely — a silent undercount. Count them so the
    # view can warn, mirroring the unmapped-orders notice. (No country filter — same
    # reasoning as above.)
    unmapped_allocations = con.execute(
        "SELECT COUNT(*) AS c FROM allocations a "
        "LEFT JOIN material_map mm ON mm.material_prefix = a.material_prefix "
        "WHERE mm.plan_model IS NULL OR mm.material_prefix IS NULL"
    ).fetchone()["c"]

    # Material prefixes mapped to each plan_model — surfaces the join for
    # the per-model story panel.
    prefixes_per_model = {}
    for r in con.execute(
        "SELECT plan_model, material_prefix FROM material_map "
        "WHERE plan_model IS NOT NULL AND status='active' "
        "ORDER BY material_prefix"
    ).fetchall():
        prefixes_per_model.setdefault(r["plan_model"], []).append(r["material_prefix"])

    # Embargoed plan models — hide entirely from dealer view.
    embargoed = {r["plan_model"] for r in con.execute(
        f"SELECT plan_model FROM embargoes "
        f"WHERE manually_hidden=1 OR (embargo_until IS NOT NULL AND embargo_until > {db.today_sql()})"
    ).fetchall()}

    # Join orders to plan rows by plan_model only.
    grid = {}        # plan_model -> {(y,m): {forecast, allocated, committed}}
    super_for = {}   # plan_model -> representative plan_super (display only)
    queue_for = {}   # plan_model -> list of orders to allocate (in creation-date order)

    for r in plan_rows:
        key = r["plan_model"]
        grid.setdefault(key, {})[(r["year"], r["month"])] = {
            "forecast": r["qty"], "allocated": 0, "committed": 0,
        }
        super_for.setdefault(key, r["plan_super"])

    unmapped_orders = 0
    orders_already_allocated = 0
    committed_by_type = {}  # plan_model -> {type: count}
    for r in order_rows:
        if r["order_number"] and r["order_number"] in allocated_order_numbers:
            # Already on the allocations sheet — counted under 'allocated', not twice.
            orders_already_allocated += 1
            continue
        if not r["plan_model"]:
            unmapped_orders += 1
            continue
        super_for.setdefault(r["plan_model"], r["plan_super"])
        queue_for.setdefault(r["plan_model"], []).append(None)  # 1 token per order; FIFO is by SQL order
        # Categorise for the per-model story panel. The WHERE clause above
        # already ensures every row counts; we just need to label it
        # without double-counting (bike_type wins when set; else End-customer).
        bt = (r["bike_type"] or "").strip()
        if bt in ("Demo", "Courtesy"):
            label = bt
        else:
            label = "End-customer"
        bucket = committed_by_type.setdefault(r["plan_model"], {})
        bucket[label] = bucket.get(label, 0) + 1

    # Ensure every month in the window has a cell, even for models with no plan forecast.
    for p_model in set(grid) | set(queue_for) | set(alloc_total):
        monthly = grid.setdefault(p_model, {})
        for ym in months_keys:
            monthly.setdefault(ym, {"forecast": 0, "allocated": 0, "committed": 0})

    # First: assign allocations (delivered bikes) chronologically off the front
    # of the forecast. Excess beyond total forecast piles up on the last month,
    # producing a negative available number — signalling overcommit.
    for p_model, monthly in grid.items():
        remaining = alloc_total.get(p_model, 0)
        for ym in months_keys:
            if remaining <= 0:
                break
            cap = monthly[ym]["forecast"]
            take = min(cap, remaining) if cap > 0 else 0
            monthly[ym]["allocated"] = take
            remaining -= take
        if remaining > 0:
            monthly[months_keys[-1]]["allocated"] += remaining

    # Then: allocate open orders FIFO into the first month with remaining
    # capacity AFTER allocations. Overflow piles onto the last month.
    for p_model, monthly in grid.items():
        for _ in queue_for.get(p_model, []):
            placed = False
            for ym in months_keys:
                free = (monthly[ym]["forecast"]
                        - monthly[ym]["allocated"]
                        - monthly[ym]["committed"])
                if free > 0:
                    monthly[ym]["committed"] += 1
                    placed = True
                    break
            if not placed:
                monthly[months_keys[-1]]["committed"] += 1

    rows = []
    for p_model, monthly in sorted(grid.items(), key=lambda x: (super_for.get(x[0]) or "", x[0])):
        if p_model in embargoed:
            continue
        cells = []
        soonest = None
        forecast_total = 0
        allocated_total = 0
        committed_total = 0
        # Bikes forecast but not yet consumed in a month don't vanish at month-end —
        # they roll into the following months. So the figure that drives the traffic
        # light is the *cumulative* available (running sum of forecast − allocated −
        # committed). Because orders fill earliest-month-first and any overflow lands
        # on the last month, interior months never go negative, so this running total
        # only dips below zero when the model is genuinely overcommitted overall.
        cumulative = 0
        for ym in months_keys:
            d = monthly.get(ym, {"forecast": 0, "allocated": 0, "committed": 0})
            available = d["forecast"] - d["allocated"] - d["committed"]
            cumulative += available
            cells.append({
                "year": ym[0], "month": ym[1],
                "forecast": d["forecast"],
                "allocated": d["allocated"],
                "committed": d["committed"],
                "available": available,
                "cumulative": cumulative,
            })
            forecast_total += d["forecast"]
            allocated_total += d["allocated"]
            committed_total += d["committed"]
            if soonest is None and cumulative >= 1:
                soonest = ym
        story = {
            "prefixes": prefixes_per_model.get(p_model, []),
            "forecast_total": forecast_total,
            "allocated_total": allocated_total,
            "committed_total": committed_total,
            "available_total": forecast_total - allocated_total - committed_total,
            "alloc_by_country": alloc_by_country.get(p_model, {}),
            "committed_by_type": committed_by_type.get(p_model, {}),
        }
        rows.append({"plan_super": super_for.get(p_model), "plan_model": p_model,
                     "cells": cells, "visible_cells": cells[:visible_n],
                     "soonest": soonest, "story": story})

    # dealer's own committed orders (skipped for the synthetic admin preview)
    own_orders = []
    if dealer["dealer_code"]:
        own_orders = con.execute(
            "SELECT order_number, bike_model, bike_color, bike_type, "
            "       end_customer_status, request_date, confirmed_delivery_date "
            "FROM orders WHERE dealer_code=? "
            "ORDER BY confirmed_delivery_date",
            (dealer["dealer_code"],),
        ).fetchall()

    last_import = con.execute(
        "SELECT imported_at FROM imports ORDER BY id DESC LIMIT 1"
    ).fetchone()

    return render_template(
        "dealer.html",
        dealer=dealer,
        rows=rows,
        months=months,
        visible_months=visible_months,
        is_admin_view=is_admin_view,
        month_names=MONTH_NAMES,
        accounting_month={
            "label": f"{MONTH_NAMES[anchor_date.month - 1]} {anchor_date.year}",
            "source": anchor_source,
        },
        own_orders=own_orders,
        last_import=last_import["imported_at"] if last_import else None,
        unmapped_orders=unmapped_orders,
        unmapped_allocations=unmapped_allocations,
        orders_already_allocated=orders_already_allocated,
    )


# ----- admin: home ----------------------------------------------------

def _latest_plan_import():
    """Most recent imports row that actually carried a plan upload."""
    return db.get_conn().execute(
        "SELECT id, imported_at, plan_filename, plan_rows FROM imports "
        "WHERE plan_filename IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()


def _latest_orders_import():
    """Most recent imports row that actually carried an orders upload."""
    return db.get_conn().execute(
        "SELECT id, imported_at, orders_filename, order_rows FROM imports "
        "WHERE orders_filename IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()


def _latest_allocations_report():
    """Most recent allocations upload (or None if there's never been one)."""
    return db.get_conn().execute(
        "SELECT id, report_date, uploaded_at, filename, row_count "
        "FROM allocation_reports ORDER BY report_date DESC, id DESC LIMIT 1"
    ).fetchone()


# Allocations report-date is resolved either from a sheet name (master
# workbook) or from the filename (single-period daily file). The shared
# parser helper handles both.
def _parse_allocations_report_date(text):
    return parsers.parse_period_text(text)


def _ingest_allocations(allocation_rows, report_date, filename):
    """Append-only dedup-by-order_number insert. Writes a row to
    allocation_reports. Caller is responsible for commit. Returns
    (added, skipped)."""
    con = db.get_conn()
    existing = {
        r["order_number"] for r in con.execute(
            "SELECT order_number FROM allocations WHERE order_number IS NOT NULL"
        ).fetchall()
    }
    rows_to_insert = []
    seen_in_file = set()
    skipped = 0
    for row in allocation_rows:
        on = row.get("order_number")
        if on and (on in existing or on in seen_in_file):
            skipped += 1
            continue
        if on:
            seen_in_file.add(on)
        rows_to_insert.append(row)
    if rows_to_insert:
        con.executemany(
            "INSERT INTO allocations(order_number, material_prefix, material_full, "
            "bike_super_model, bike_model, country, dealer_code) "
            "VALUES(:order_number,:material_prefix,:material_full,"
            ":bike_super_model,:bike_model,:country,:dealer_code)",
            rows_to_insert,
        )
    con.execute(
        "INSERT INTO allocation_reports(report_date, filename, row_count) "
        "VALUES(?,?,?)",
        (report_date, filename, len(rows_to_insert)),
    )
    return len(rows_to_insert), skipped


# Material-prefix → plan-model auto-suggestion. Proposals are written to
# material_map.proposed_plan_* and surfaced on the mapping page for admin
# review; nothing is auto-applied.

# Keep '+' so 'Monster +' stays distinguishable from 'Monster' through norm.
_NORM_PUNCT_RE = re.compile(r"[^a-z0-9+]+")
_PARENS_RE = re.compile(r"\s*\([^)]*\)")

# Bidirectional abbreviation expansions. Applied before the alphanumeric
# squash so that "MTS V4" and "Multistrada V4" both normalize to "mtsv4".
# The long form is replaced by the short form (one direction is enough as
# long as both sides of any potential match get normalized the same way).
# Longer phrases are listed first so they match before any of their
# constituent words.
_ABBREV = [
    # multi-word phrases first
    ("pikes peak",   "pp"),
    ("pikespeak",    "pp"),
    ("desert x",     "dsx"),
    # single-word
    ("multistrada",  "mts"),
    ("streetfighter", "sf"),
    ("hypermotard",  "hym"),
    ("xdiavel",      "xdvl"),
    ("monster",      "mon"),
    ("panigale",     "pan"),
    ("diavel",       "dvl"),
    ("desertx",      "dsx"),
    ("scrambler",    "scr"),
    # punctuation-style synonyms
    ("plus",         "+"),
]


def _norm(s):
    """Aggressive normalize for fuzzy name matching: lowercase, expand model
    abbreviations, drop everything non-alphanumeric."""
    if s is None:
        return ""
    s = str(s).lower()
    for long_form, short_form in _ABBREV:
        s = s.replace(long_form, short_form)
    return _NORM_PUNCT_RE.sub("", s)


_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9+]+")


def _tokens(s):
    """Split a model name into normalized tokens. Lowercases, applies the
    same abbreviation table as _norm, then splits on whitespace and
    punctuation. Used by the token-set match tier — lets 'HYM SP' match a
    bike named 'Hypermotard V2 SP' even though the tokens aren't
    contiguous in the squashed string."""
    if s is None:
        return ()
    s = str(s).lower()
    for long_form, short_form in _ABBREV:
        s = s.replace(long_form, short_form)
    return tuple(t for t in _TOKEN_SPLIT_RE.split(s) if t)


def _name_variants(name):
    """Yield reasonable alternative forms of a model name so a single plan
    entry like 'MTS V4 S / MTS V4 S MTO' matches either side, and
    'Desert X (896)' matches both with and without the parenthetical."""
    if not name:
        return []
    seen = set()
    variants = []

    def _add(v):
        v = (v or "").strip()
        if v and v not in seen:
            seen.add(v)
            variants.append(v)

    _add(name)
    # Split on '/' (slash with optional surrounding whitespace) — common for
    # plan_models that bundle a couple of SKUs into one row.
    for part in re.split(r"\s*/\s*", name):
        _add(part)
    # Strip parenthetical bits like "(896)".
    stripped = _PARENS_RE.sub("", name).strip()
    _add(stripped)
    return variants


def _auto_suggest_mappings():
    """For every status='unmapped' row in material_map that hasn't been
    rejected, look at its orders' (bike_super_model, bike_model) values and
    try to find a matching (plan_super, plan_model) in the current plan.
    Writes the match (if any) to proposed_plan_super / proposed_plan_model.
    Returns the count of new proposals made."""
    con = db.get_conn()

    plan_pairs = con.execute(
        "SELECT DISTINCT plan_super, plan_model FROM plan WHERE plan_model IS NOT NULL"
    ).fetchall()
    if not plan_pairs:
        return 0

    # Index every (plan_super, plan_model) under its name variants.
    # `by_super_model`: keyed on (norm_super, norm_model_variant)
    # `by_model`:       keyed on norm_model_variant — ambiguous keys are
    #                   removed so we never blindly pick one of two plans.
    # `super_index`:    map norm(plan_super) → set of plans for substring
    #                   fallback (so we can scope substring matches to the
    #                   right product family when possible).
    by_super_model = {}
    by_model = {}
    super_index = {}
    _model_seen = set()
    _model_ambig = set()
    all_plans = set()
    for r in plan_pairs:
        ps, pm = r["plan_super"], r["plan_model"]
        all_plans.add((ps, pm))
        super_index.setdefault(_norm(ps), set()).add((ps, pm))
        for variant in _name_variants(pm):
            key = _norm(variant)
            by_super_model[(_norm(ps), key)] = (ps, pm)
            if key in _model_seen and by_model.get(key) != (ps, pm):
                _model_ambig.add(key)
            else:
                _model_seen.add(key)
                by_model[key] = (ps, pm)
    for key in _model_ambig:
        by_model.pop(key, None)

    candidates = con.execute(
        "SELECT material_prefix FROM material_map "
        "WHERE status='unmapped' AND proposal_rejected=0"
    ).fetchall()

    n = 0
    for c in candidates:
        prefix = c["material_prefix"]
        # Try every distinct (super, model) pair we've seen for this prefix,
        # not just one — different colour/spec orders may name the bike a bit
        # differently and one might match cleanly.
        orders = con.execute(
            "SELECT DISTINCT bike_super_model, bike_model FROM orders "
            "WHERE material_prefix=? AND bike_model IS NOT NULL",
            (prefix,),
        ).fetchall()
        match = None
        for o in orders:
            bs = o["bike_super_model"]
            bm = o["bike_model"]
            for bm_variant in _name_variants(bm):
                bm_key = _norm(bm_variant)
                # Tier 1: exact (super, model) match
                match = by_super_model.get((_norm(bs), bm_key))
                if match:
                    break
                # Tier 2: exact model-only match (unambiguous)
                m2 = by_model.get(bm_key)
                if m2:
                    match = m2
                    break
            if match:
                break

        # Candidate plan set (shared by token + substring tiers): prefer
        # plans whose super exactly matches the bike super; only fall back
        # to loose-super plans if no exact-super candidate exists. This
        # avoids cases where the same model exists under two supers (e.g.
        # 'Panigale' and 'Panigale V4') and would otherwise be ambiguous.
        def _candidate_plans_for(bs_norm):
            ex = set(); loose = set()
            for plan_super_norm, plans in super_index.items():
                if not plan_super_norm or not bs_norm:
                    continue
                if plan_super_norm == bs_norm:
                    ex |= plans
                elif (plan_super_norm in bs_norm
                      or bs_norm in plan_super_norm):
                    loose |= plans
            return ex or loose or all_plans

        # Tier 3: token-set match. Only fires when one side is fully
        # contained in the other (plan ⊆ bike OR bike ⊆ plan) — partial
        # overlap is too permissive and would grab generic plans for
        # specific bikes (e.g. picking 'MTS V2' for 'Multistrada V2S
        # Travel' on a single shared 'mts' token).
        # Among full-inclusion candidates, score by (tokens shared,
        # −extra plan tokens). Highest shared count wins; ties broken by
        # the plan with the fewest extras. So:
        #   - 'HYM SP' [hym, sp] vs bike 'Hypermotard V2 SP' [hym, v2, sp]
        #     (plan ⊆ bike, 2 shared, 0 extra) beats 'HYM' (1 shared).
        #   - 'Panigale V2 (896) S FB63' [pan, v2, 896, s, fb63] vs bike
        #     'Panigale V2 FB63' [pan, v2, fb63] (bike ⊆ plan, 3 shared,
        #     2 extra) beats 'Panigale V2' (2 shared, 0 extra).
        if match is None:
            for o in orders:
                bs = o["bike_super_model"]
                bm = o["bike_model"]
                candidate_plans = _candidate_plans_for(_norm(bs))
                for bm_variant in _name_variants(bm):
                    bm_tokens = set(_tokens(bm_variant))
                    if not bm_tokens:
                        continue
                    hits = []
                    for ps, pm in candidate_plans:
                        best_for_plan = None
                        for pm_variant in _name_variants(pm):
                            pm_tokens = set(_tokens(pm_variant))
                            if not pm_tokens:
                                continue
                            # Require full inclusion in either direction
                            # — partial overlap is too noisy.
                            if not (pm_tokens <= bm_tokens
                                    or bm_tokens <= pm_tokens):
                                continue
                            shared = len(pm_tokens & bm_tokens)
                            extra = len(pm_tokens - bm_tokens)
                            score = (shared, -extra)
                            if best_for_plan is None or score > best_for_plan:
                                best_for_plan = score
                        if best_for_plan is not None:
                            hits.append((best_for_plan, ps, pm))
                    if hits:
                        hits.sort(reverse=True)
                        top_score = hits[0][0]
                        winners = {(h[1], h[2]) for h in hits if h[0] == top_score}
                        if len(winners) == 1:
                            match = winners.pop()
                            break
                if match:
                    break

        # Tier 4: substring match. Two cases:
        #   A) plan ⊂ bike  — the plan rolls up a specific bike. Want the
        #      LONGEST plan that fits in the bike (most informative).
        #   B) bike ⊂ plan  — the plan name has extra suffixes (gen markers,
        #      special editions). Want the SHORTEST plan that contains the
        #      bike — picking the longest would silently upgrade 'Nightshift'
        #      to 'Nightshift Centenario'.
        _MIN_SUBSTR_LEN = 3
        if match is None:
            for o in orders:
                bs = o["bike_super_model"]
                bm = o["bike_model"]
                candidate_plans = _candidate_plans_for(_norm(bs))

                for bm_variant in _name_variants(bm):
                    bm_norm = _norm(bm_variant)
                    if len(bm_norm) < _MIN_SUBSTR_LEN:
                        continue
                    a_hits = []  # plan ⊂ bike — longest wins
                    b_hits = []  # bike ⊂ plan — shortest wins
                    for ps, pm in candidate_plans:
                        for pm_variant in _name_variants(pm):
                            pm_norm = _norm(pm_variant)
                            if len(pm_norm) < _MIN_SUBSTR_LEN:
                                continue
                            if pm_norm == bm_norm:
                                a_hits.append((len(pm_norm), ps, pm))
                                break
                            if pm_norm in bm_norm:
                                a_hits.append((len(pm_norm), ps, pm))
                                break
                            if bm_norm in pm_norm:
                                b_hits.append((len(pm_norm), ps, pm))
                                break
                    if a_hits:
                        a_hits.sort(reverse=True)  # longest first
                        top_len = a_hits[0][0]
                        top_set = {(h[1], h[2]) for h in a_hits if h[0] == top_len}
                        if len(top_set) == 1:
                            match = top_set.pop()
                            break
                    if b_hits:
                        b_hits.sort()  # shortest first
                        top_len = b_hits[0][0]
                        top_set = {(h[1], h[2]) for h in b_hits if h[0] == top_len}
                        if len(top_set) == 1:
                            match = top_set.pop()
                            break
                if match:
                    break

        if match is None:
            continue
        con.execute(
            "UPDATE material_map SET proposed_plan_super=?, proposed_plan_model=? "
            "WHERE material_prefix=?",
            (match[0], match[1], prefix),
        )
        n += 1
    return n


@app.route("/admin")
@admin_required
def admin_home():
    con = db.get_conn()
    last_plan = _latest_plan_import()
    last_orders = _latest_orders_import()
    last_allocations = _latest_allocations_report()
    last_any = con.execute(
        "SELECT imported_at FROM imports ORDER BY id DESC LIMIT 1"
    ).fetchone()
    unmapped = con.execute(
        "SELECT COUNT(*) AS c FROM material_map WHERE status='unmapped'"
    ).fetchone()["c"]
    dealer_count = con.execute("SELECT COUNT(*) AS c FROM dealers").fetchone()["c"]
    order_count = con.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
    plan_count = con.execute("SELECT COUNT(*) AS c FROM plan").fetchone()["c"]

    # "Embargo may be over" — currently-hidden plan_models that have open orders
    # against them (orders are flowing => probably no longer embargoed in reality).
    embargo_review = con.execute(
        f"SELECT e.plan_model, COUNT(o.id) AS order_count "
        f"FROM embargoes e "
        f"JOIN material_map mm ON mm.plan_model = e.plan_model "
        f"JOIN orders o ON o.material_prefix = mm.material_prefix "
        f"WHERE (e.manually_hidden=1 OR (e.embargo_until IS NOT NULL "
        f"       AND e.embargo_until > {db.today_sql()})) "
        f"GROUP BY e.plan_model "
        f"ORDER BY order_count DESC"
    ).fetchall()

    # "New bikes" — plan_models that appeared in an import and haven't been
    # acknowledged by the admin yet. Excludes any already on the embargoes list.
    new_models = con.execute(
        "SELECT pmh.plan_model "
        "FROM plan_model_history pmh "
        "LEFT JOIN embargoes e ON e.plan_model = pmh.plan_model "
        "WHERE pmh.acknowledged=0 AND e.plan_model IS NULL "
        "ORDER BY pmh.first_seen_at DESC, pmh.plan_model"
    ).fetchall()

    return render_template(
        "admin_home.html",
        last_plan=last_plan,
        last_orders=last_orders,
        last_allocations=last_allocations,
        last_any_at=last_any["imported_at"] if last_any else None,
        unmapped=unmapped,
        dealer_count=dealer_count,
        order_count=order_count,
        plan_count=plan_count,
        embargo_review=embargo_review,
        new_models=new_models,
    )


@app.route("/admin/notify/lift-embargo", methods=["POST"])
@admin_required
def admin_notify_lift_embargo():
    db.get_conn().execute("DELETE FROM embargoes WHERE plan_model=?", (request.form["plan_model"],))
    db.get_conn().commit()
    return redirect(url_for("admin_home"))


@app.route("/admin/notify/embargo-new", methods=["POST"])
@admin_required
def admin_notify_embargo_new():
    pm = request.form["plan_model"]
    con = db.get_conn()
    con.execute(
        "INSERT INTO embargoes(plan_model, manually_hidden) VALUES(?, 1) "
        "ON CONFLICT(plan_model) DO UPDATE SET manually_hidden=1, "
        "updated_at=CURRENT_TIMESTAMP",
        (pm,),
    )
    con.execute("UPDATE plan_model_history SET acknowledged=1 WHERE plan_model=?", (pm,))
    con.commit()
    return redirect(url_for("admin_home"))


@app.route("/admin/notify/dismiss-new", methods=["POST"])
@admin_required
def admin_notify_dismiss_new():
    con = db.get_conn()
    con.execute("UPDATE plan_model_history SET acknowledged=1 WHERE plan_model=?",
                (request.form["plan_model"],))
    con.commit()
    return redirect(url_for("admin_home"))


@app.route("/admin/help")
@admin_required
def admin_help():
    return render_template("admin_help.html")


# ----- api: allocations ingest (for email-to-webhook automation) ------

@app.route("/api/allocations/ingest", methods=["POST"])
def api_allocations_ingest():
    """Accept an Allocations xlsx attachment over HTTP and apply it the same
    way the admin upload would. Designed for an email-to-webhook service
    (e.g. CloudMailin) forwarding allocation emails from Outlook.

    Auth: header `X-API-Key` must equal env var ALLOCATIONS_INGEST_KEY.
    Body: any uploaded file ending in .xlsx is taken as the allocations
          file. The report date is parsed from its filename; the request
          may override it via form field `report_date` or JSON
          `report_date`.

    Responses:
      200 {status:'ok', added, skipped, report_date, filename}
      200 {status:'rejected_older', current, attempted}
        - older-date rejection; 200 so the webhook service doesn't retry
      400 parse error / no attachment
      401 missing or wrong API key
      503 server not configured (env var absent)
    """
    expected_key = (os.environ.get("ALLOCATIONS_INGEST_KEY") or "").strip()
    if not expected_key:
        return jsonify({
            "status": "server_misconfigured",
            "detail": "ALLOCATIONS_INGEST_KEY env var not set",
        }), 503
    presented = request.headers.get("X-API-Key", "").strip()
    if presented != expected_key:
        return jsonify({"status": "unauthorized"}), 401

    # Find the first .xlsx attachment regardless of form field name.
    upload = None
    for _key, f in request.files.items(multi=True):
        if f and f.filename and f.filename.lower().endswith(".xlsx"):
            upload = f
            break
    if upload is None:
        return jsonify({
            "status": "no_attachment",
            "detail": "No .xlsx attachment found in request.files",
        }), 400

    # Optional date override via form/json — applied when a sheet has no
    # date in its own name and the filename also doesn't match.
    override = (request.form.get("report_date")
                or (request.get_json(silent=True) or {}).get("report_date")
                or "").strip()

    try:
        sheets, alloc_warnings = parsers.parse_allocations(io.BytesIO(upload.stream.read()))
    except Exception as e:
        return jsonify({
            "status": "parse_failed",
            "detail": f"{type(e).__name__}: {e}",
        }), 400

    if not sheets:
        return jsonify({
            "status": "no_data",
            "detail": "Workbook had no allocation rows.",
        }), 400

    filename_date = _parse_allocations_report_date(upload.filename)
    resolved = []
    for sheet_name, sheet_date, rows in sheets:
        effective_date = sheet_date or override or filename_date
        if not effective_date:
            return jsonify({
                "status": "bad_date",
                "detail": (f"Couldn't parse report_date for sheet "
                           f"'{sheet_name}'. Filename '{upload.filename}' "
                           f"doesn't match 'Allocations DD.MM.YY am|pm.xlsx' "
                           f"either. Send a `report_date` field to override."),
            }), 400
        resolved.append((sheet_name, effective_date, rows))
    resolved.sort(key=lambda x: x[1])

    # Single-sheet older-date rejection (same defensive net as the admin
    # upload). Multi-sheet uploads bypass this — backfilling history is a
    # legitimate use of the ingest endpoint too.
    last = _latest_allocations_report()
    if len(resolved) == 1 and last and resolved[0][1] < last["report_date"]:
        return jsonify({
            "status": "rejected_older",
            "current": last["report_date"],
            "attempted": resolved[0][1],
            "filename": upload.filename,
        }), 200

    total_added = 0
    total_skipped = 0
    sheet_summaries = []
    for sheet_name, effective_date, rows in resolved:
        label = (upload.filename
                 if len(resolved) == 1
                 else f"{upload.filename} [{sheet_name}]")
        added, skipped = _ingest_allocations(rows, effective_date, label)
        total_added += added
        total_skipped += skipped
        sheet_summaries.append({
            "report_date": effective_date,
            "added": added,
            "skipped": skipped,
        })
    db.get_conn().commit()
    return jsonify({
        "status": "ok",
        "added": total_added,
        "skipped": total_skipped,
        "sheets": sheet_summaries,
        "warnings": alloc_warnings,
        "report_date": resolved[-1][1],
        "filename": upload.filename,
    })


# ----- admin: upload --------------------------------------------------

@app.route("/admin/upload", methods=["GET", "POST"])
@admin_required
def admin_upload():
    if request.method == "GET":
        return render_template(
            "admin_upload.html",
            last_plan=_latest_plan_import(),
            last_orders=_latest_orders_import(),
            last_allocations=_latest_allocations_report(),
            accounting_month=_accounting_month_state(),
        )

    # Per-file action: "new" = upload a fresh file, "reuse" = keep what's in the DB.
    # Default to "new" if the form somehow doesn't send it.
    plan_action = (request.form.get("plan_action") or "new").strip()
    orders_action = (request.form.get("orders_action") or "new").strip()
    allocations_action = (request.form.get("allocations_action") or "reuse").strip()
    plan_file = request.files.get("plan_file") if plan_action == "new" else None
    orders_file = request.files.get("orders_file") if orders_action == "new" else None
    allocations_file = (request.files.get("allocations_file")
                        if allocations_action == "new" else None)
    # Allocations report date — derived from filename, with admin override field.
    allocations_date_override = (request.form.get("allocations_date") or "").strip()
    # "Replace all" — clear the allocations table before ingesting this file, for a
    # clean rebuild without a plan re-upload. Only meaningful when uploading a file.
    allocations_replace = bool(request.form.get("allocations_replace")) and allocations_file is not None

    last_alloc = _latest_allocations_report()

    # Validate combinations.
    if plan_action == "reuse" and not _latest_plan_import():
        flash("Can't reuse plan data — none has ever been uploaded.", "error")
        return redirect(url_for("admin_upload"))
    if orders_action == "reuse" and not _latest_orders_import():
        flash("Can't reuse orders data — none has ever been uploaded.", "error")
        return redirect(url_for("admin_upload"))
    if allocations_action == "reuse" and not last_alloc:
        # No previous allocations is fine — treat reuse as "no allocations yet".
        # Just continue without doing anything for that file.
        pass
    if plan_action == "new" and (not plan_file or not plan_file.filename):
        flash("Plan file not attached.", "error")
        return redirect(url_for("admin_upload"))
    if orders_action == "new" and (not orders_file or not orders_file.filename):
        flash("Orders file not attached.", "error")
        return redirect(url_for("admin_upload"))
    if allocations_action == "new" and (not allocations_file
                                        or not allocations_file.filename):
        flash("Allocations file not attached.", "error")
        return redirect(url_for("admin_upload"))
    if (plan_action == "reuse" and orders_action == "reuse"
            and allocations_action != "new"):
        flash("Nothing to import — choose at least one file to refresh.", "error")
        return redirect(url_for("admin_upload"))

    # Parse only the side(s) the user is refreshing.
    plan_rows = None
    order_rows = None
    allocation_sheets = None
    alloc_warnings = []
    try:
        if plan_file is not None:
            plan_rows = parsers.parse_plan(io.BytesIO(plan_file.stream.read()))
        if orders_file is not None:
            order_rows = parsers.parse_orders(io.BytesIO(orders_file.stream.read()))
        if allocations_file is not None:
            # (sheets, warnings); sheets are (sheet_name, report_date_or_None, rows).
            allocation_sheets, alloc_warnings = parsers.parse_allocations(
                io.BytesIO(allocations_file.stream.read())
            )
    except Exception as e:
        flash(f"Parse failed: {e}", "error")
        return redirect(url_for("admin_upload"))

    # Resolve effective report dates for each allocations sheet. Sheet-name
    # dates win (master workbook); otherwise fall back to the filename, then
    # the admin override. For multi-sheet files we skip the older-date
    # rejection — backfilling old periods is a legitimate use of master
    # uploads, and the order_number dedup makes it safe.
    resolved_sheets = []
    if allocation_sheets is not None and allocations_file is not None:
        if not allocation_sheets:
            flash("Allocations workbook had no data sheets.", "error")
            return redirect(url_for("admin_upload"))
        filename_date = _parse_allocations_report_date(allocations_file.filename)
        for sheet_name, sheet_date, rows in allocation_sheets:
            effective_date = (sheet_date
                              or allocations_date_override
                              or filename_date)
            if not effective_date:
                flash(
                    f"Couldn't read a report date for sheet '{sheet_name}'. "
                    f"Rename it to 'DD.MM.YY am|pm', rename the file to "
                    f"'Allocations DD.MM.YY am|pm.xlsx', or type a date "
                    f"into the override field.",
                    "error",
                )
                return redirect(url_for("admin_upload"))
            resolved_sheets.append((sheet_name, effective_date, rows))
        # Sort chronologically so the audit log timeline reads correctly.
        resolved_sheets.sort(key=lambda x: x[1])
        # Single-sheet upload: keep the older-date rejection as a sanity net
        # against accidentally re-sending yesterday's file.
        if len(resolved_sheets) == 1 and last_alloc:
            only_date = resolved_sheets[0][1]
            if only_date < last_alloc["report_date"]:
                flash(
                    f"Allocations file is older than the current snapshot "
                    f"({last_alloc['report_date']}). Upload skipped to avoid "
                    f"rolling back. If this is intentional, override the date "
                    f"to a newer one.",
                    "error",
                )
                return redirect(url_for("admin_upload"))

    con = db.get_conn()

    # Plan side.
    new_prefixes = set()
    new_models = set()
    plan_wiped_allocations = 0
    if plan_rows is not None:
        # Snapshot the existing plan_models so we can diff against the new upload.
        previous_models = {r["plan_model"] for r in con.execute(
            "SELECT DISTINCT plan_model FROM plan WHERE plan_model IS NOT NULL"
        ).fetchall()}
        # A new plan = new accounting period for allocations. Wipe the table.
        plan_wiped_allocations = con.execute(
            "SELECT COUNT(*) AS c FROM allocations"
        ).fetchone()["c"]
        con.execute("DELETE FROM allocations")
        con.execute("DELETE FROM allocation_reports")
        # New accounting period — drop any manual accounting-month pin so it
        # can't leak the old month into the new plan's window. (Same connection,
        # no commit here — the whole upload commits together at the end.)
        con.execute("DELETE FROM settings WHERE key='accounting_month'")
        con.execute("DELETE FROM plan")
        con.executemany(
            "INSERT INTO plan(country, plan_super, plan_model, year, month, qty) "
            "VALUES(?,?,?,?,?,?)",
            plan_rows,
        )

        # Models that weren't in the previous plan are flagged for admin review.
        # On the very first upload (previous_models is empty), nothing is "new" —
        # we pre-acknowledge everything so the notification panel stays empty
        # until something actually changes between uploads.
        seen_models = {p[2] for p in plan_rows}
        first_upload = not previous_models
        new_models = (seen_models - previous_models) if not first_upload else set()
        pre_ack_models = seen_models if first_upload else (seen_models - new_models)
        for m in sorted(new_models):
            con.execute(
                "INSERT INTO plan_model_history(plan_model, acknowledged) VALUES(?, 0) "
                "ON CONFLICT(plan_model) DO NOTHING",
                (m,),
            )
        for m in sorted(pre_ack_models):
            con.execute(
                "INSERT INTO plan_model_history(plan_model, acknowledged) VALUES(?, 1) "
                "ON CONFLICT(plan_model) DO NOTHING",
                (m,),
            )

    # Orders side.
    if order_rows is not None:
        con.execute("DELETE FROM orders")
        con.executemany(
            "INSERT INTO orders(order_number, material_prefix, material_full, bike_super_model, "
            "bike_model, bike_color, bike_type, end_customer_status, country, dealer, dealer_code, "
            "request_date, order_creation_date, confirmed_delivery_date, order_status_group) "
            "VALUES(:order_number,:material_prefix,:material_full,:bike_super_model,:bike_model,"
            ":bike_color,:bike_type,:end_customer_status,:country,:dealer,:dealer_code,"
            ":request_date,:order_creation_date,:confirmed_delivery_date,:order_status_group)",
            order_rows,
        )

        # auto-add any unseen Material prefixes to material_map with status='unmapped'
        seen_prefixes = {o["material_prefix"] for o in order_rows if o["material_prefix"]}
        existing = {r["material_prefix"] for r in con.execute(
            "SELECT material_prefix FROM material_map").fetchall()}
        new_prefixes = seen_prefixes - existing
        for p in sorted(new_prefixes):
            con.execute(
                "INSERT INTO material_map(material_prefix, status) VALUES(?, 'unmapped')",
                (p,),
            )

    # Allocations side. Append-only with order_number dedup — each sheet isn't
    # cumulative but the DB needs to be, so each upload contributes only its
    # not-yet-seen rows. A new plan upload (above) wipes the table to start
    # the next accounting period fresh.
    allocations_added = 0
    allocations_skipped = 0
    allocations_replaced = 0
    if resolved_sheets and allocations_file is not None:
        if allocations_replace:
            # Clean rebuild: drop everything before loading this file from scratch.
            allocations_replaced = con.execute(
                "SELECT COUNT(*) AS c FROM allocations"
            ).fetchone()["c"]
            con.execute("DELETE FROM allocations")
            con.execute("DELETE FROM allocation_reports")
        for sheet_name, effective_date, rows in resolved_sheets:
            # Label the audit-log filename with the sheet name when we're
            # ingesting more than one (so a master upload doesn't look like
            # 28 identical rows in allocation_reports).
            label = (allocations_file.filename
                     if len(resolved_sheets) == 1
                     else f"{allocations_file.filename} [{sheet_name}]")
            added, skipped = _ingest_allocations(rows, effective_date, label)
            allocations_added += added
            allocations_skipped += skipped

        # Register any new Material prefixes seen in the allocations, so they show
        # up on the Mapping page and stop being silently dropped from the allocated
        # figures. Orders did this above; allocations were missing it.
        seen_alloc_prefixes = {
            row.get("material_prefix")
            for _sn, _dt, sheet_rows in resolved_sheets
            for row in sheet_rows
            if row.get("material_prefix")
        }
        existing_prefixes = {r["material_prefix"] for r in con.execute(
            "SELECT material_prefix FROM material_map").fetchall()}
        for p in sorted(seen_alloc_prefixes - existing_prefixes):
            con.execute(
                "INSERT INTO material_map(material_prefix, status) VALUES(?, 'unmapped')",
                (p,),
            )

    # Auto-suggest mappings for unmapped prefixes. Cheap to always run — picks
    # up new prefixes from an orders refresh AND new matches enabled by a plan
    # refresh. Suggestions are proposals only; the admin still confirms.
    proposed_n = _auto_suggest_mappings()

    unmapped_total = con.execute(
        "SELECT COUNT(*) AS c FROM material_map WHERE status='unmapped' "
        "AND (material_prefix IN (SELECT DISTINCT material_prefix FROM orders) "
        "  OR material_prefix IN (SELECT DISTINCT material_prefix FROM allocations))"
    ).fetchone()["c"]

    con.execute(
        "INSERT INTO imports(plan_filename, orders_filename, plan_rows, order_rows, unmapped_count) "
        "VALUES(?,?,?,?,?)",
        (
            plan_file.filename if plan_file is not None else None,
            orders_file.filename if orders_file is not None else None,
            len(plan_rows) if plan_rows is not None else None,
            len(order_rows) if order_rows is not None else None,
            unmapped_total,
        ),
    )
    con.commit()

    parts = []
    if plan_rows is not None:
        parts.append(f"{len(plan_rows)} plan rows")
    else:
        parts.append("plan reused")
    if order_rows is not None:
        parts.append(f"{len(order_rows)} orders")
    else:
        parts.append("orders reused")
    if resolved_sheets:
        bits = [f"+{allocations_added} allocations"]
        if allocations_skipped:
            bits.append(f"{allocations_skipped} duplicate(s) skipped")
        if len(resolved_sheets) == 1:
            bits.append(f"report {resolved_sheets[0][1]}")
        else:
            bits.append(
                f"{len(resolved_sheets)} sheets, "
                f"{resolved_sheets[0][1]} → {resolved_sheets[-1][1]}"
            )
        parts.append(" ".join(bits))
    elif last_alloc:
        parts.append("allocations kept")
    else:
        parts.append("no allocations")
    msg = "Imported: " + ", ".join(parts) + "."
    if plan_wiped_allocations:
        msg += (f" Allocations table wiped ({plan_wiped_allocations} row(s) "
                f"cleared) because a new plan was uploaded.")
    if allocations_replaced:
        msg += (f" Replaced all allocations ({allocations_replaced} row(s) "
                f"cleared first, then reloaded from this file).")
    if new_prefixes:
        msg += f" {len(new_prefixes)} new Material prefix(es) detected."
    if proposed_n:
        msg += f" {proposed_n} mapping suggestion(s) ready for review."
    flash(msg, "ok")
    # Surface any non-standard-sheet warnings from the allocations parse so the
    # admin sees dropped/recovered rows instead of a silently-wrong total.
    for w in alloc_warnings:
        flash(f"Allocations sheet note — {w}", "error")
    return redirect(url_for("admin_home"))


# ----- admin: mapping -------------------------------------------------

@app.route("/admin/mapping", methods=["GET"])
@admin_required
def admin_mapping():
    con = db.get_conn()
    # Unmapped rows first, then mapped, then ignored. Within each group, those
    # with a pending proposal sort to the top so they're easy to action.
    rows = con.execute(
        f"SELECT mm.*, "
        f"(SELECT COUNT(*) FROM orders o WHERE o.material_prefix = mm.material_prefix) AS order_count, "
        f"(SELECT COUNT(*) FROM allocations a WHERE a.material_prefix = mm.material_prefix) AS allocation_count, "
        f"(SELECT {db.group_concat_distinct('bike_super_model')} FROM orders o "
        f"  WHERE o.material_prefix = mm.material_prefix) AS bike_supers, "
        f"(SELECT {db.group_concat_distinct('bike_model')} FROM orders o "
        f"  WHERE o.material_prefix = mm.material_prefix) AS bike_models "
        f"FROM material_map mm "
        f"ORDER BY CASE status WHEN 'unmapped' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, "
        f"CASE WHEN mm.proposed_plan_model IS NOT NULL THEN 0 ELSE 1 END, "
        f"mm.material_prefix"
    ).fetchall()
    plan_models = con.execute(
        "SELECT DISTINCT plan_super, plan_model FROM plan "
        "ORDER BY plan_super, plan_model"
    ).fetchall()
    proposal_count = con.execute(
        "SELECT COUNT(*) AS c FROM material_map WHERE proposed_plan_model IS NOT NULL"
    ).fetchone()["c"]
    latest_backup = con.execute(
        "SELECT snapshot_at, row_count FROM material_map_backups "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()

    # Surface plan_models that have more than one active material prefix
    # pointing at them — almost always a sign the matcher (or admin) picked
    # a too-generic plan when more specific options exist. Listed so the
    # admin can split them up.
    dup_rows = con.execute(
        "SELECT plan_super, plan_model, "
        + db.group_concat_distinct("material_prefix")
        + " AS prefixes, COUNT(*) AS n "
        "FROM material_map "
        "WHERE status='active' AND plan_model IS NOT NULL "
        "GROUP BY plan_super, plan_model "
        "HAVING COUNT(*) > 1 "
        "ORDER BY plan_super, plan_model"
    ).fetchall()
    duplicates = [
        {
            "plan_super": r["plan_super"],
            "plan_model": r["plan_model"],
            "prefixes": (r["prefixes"] or "").split(","),
            "n": r["n"],
        }
        for r in dup_rows
    ]

    return render_template("admin_mapping.html", rows=rows, plan_models=plan_models,
                           proposal_count=proposal_count, latest_backup=latest_backup,
                           duplicates=duplicates)


@app.route("/admin/mapping/update", methods=["POST"])
@admin_required
def admin_mapping_update():
    prefix = request.form["prefix"]
    plan_combo = request.form.get("plan_combo", "")
    status = request.form.get("status", "active")
    if status == "ignored":
        plan_super, plan_model = None, None
    elif plan_combo:
        plan_super, plan_model = plan_combo.split(" || ", 1)
        status = "active"
    else:
        plan_super, plan_model = None, None
        status = "unmapped"
    db.get_conn().execute(
        "UPDATE material_map SET plan_super=?, plan_model=?, status=?, "
        "proposed_plan_super=NULL, proposed_plan_model=NULL, "
        "updated_at=CURRENT_TIMESTAMP WHERE material_prefix=?",
        (plan_super, plan_model, status, prefix),
    )
    db.get_conn().commit()
    # Auto-save uses fetch with X-Requested-With; reply with JSON so the
    # page can show its "Saved" indicator without navigating away.
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({
            "status": "ok",
            "material_prefix": prefix,
            "plan_super": plan_super,
            "plan_model": plan_model,
            "row_status": status,
        })
    return redirect(url_for("admin_mapping"))


@app.route("/admin/mapping/accept", methods=["POST"])
@admin_required
def admin_mapping_accept():
    prefix = request.form["prefix"]
    con = db.get_conn()
    con.execute(
        "UPDATE material_map SET "
        "plan_super=proposed_plan_super, plan_model=proposed_plan_model, "
        "status='active', proposed_plan_super=NULL, proposed_plan_model=NULL, "
        "proposal_rejected=0, updated_at=CURRENT_TIMESTAMP "
        "WHERE material_prefix=? AND proposed_plan_model IS NOT NULL",
        (prefix,),
    )
    con.commit()
    return redirect(url_for("admin_mapping"))


@app.route("/admin/mapping/reject", methods=["POST"])
@admin_required
def admin_mapping_reject():
    prefix = request.form["prefix"]
    con = db.get_conn()
    con.execute(
        "UPDATE material_map SET "
        "proposed_plan_super=NULL, proposed_plan_model=NULL, "
        "proposal_rejected=1, updated_at=CURRENT_TIMESTAMP "
        "WHERE material_prefix=?",
        (prefix,),
    )
    con.commit()
    return redirect(url_for("admin_mapping"))


@app.route("/admin/mapping/accept-all", methods=["POST"])
@admin_required
def admin_mapping_accept_all():
    con = db.get_conn()
    cur = con.execute(
        "UPDATE material_map SET "
        "plan_super=proposed_plan_super, plan_model=proposed_plan_model, "
        "status='active', proposed_plan_super=NULL, proposed_plan_model=NULL, "
        "proposal_rejected=0, updated_at=CURRENT_TIMESTAMP "
        "WHERE proposed_plan_model IS NOT NULL"
    )
    n = cur.rowcount
    con.commit()
    flash(f"Accepted {n} suggestion(s).", "ok")
    return redirect(url_for("admin_mapping"))


@app.route("/admin/mapping/delete", methods=["POST"])
@admin_required
def admin_mapping_delete():
    prefix = request.form["prefix"]
    db.get_conn().execute("DELETE FROM material_map WHERE material_prefix=?", (prefix,))
    db.get_conn().commit()
    return redirect(url_for("admin_mapping"))


@app.route("/admin/mapping/dump", methods=["GET"])
@admin_required
def admin_mapping_dump():
    """Plain-text dump of the mapping table for debugging the auto-suggester.
    One row per material_prefix; cells separated by ' | '."""
    from flask import Response
    con = db.get_conn()
    rows = con.execute(
        f"SELECT mm.material_prefix, mm.status, "
        f"  mm.plan_super, mm.plan_model, "
        f"  mm.proposed_plan_super, mm.proposed_plan_model, "
        f"  (SELECT {db.group_concat_distinct('bike_super_model')} FROM orders o "
        f"     WHERE o.material_prefix = mm.material_prefix) AS bike_supers, "
        f"  (SELECT {db.group_concat_distinct('bike_model')} FROM orders o "
        f"     WHERE o.material_prefix = mm.material_prefix) AS bike_models "
        f"FROM material_map mm "
        f"ORDER BY CASE status WHEN 'unmapped' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, "
        f"mm.material_prefix"
    ).fetchall()
    header = ("prefix | status | plan_super | plan_model | "
              "proposed_super | proposed_model | bike_supers | bike_models")
    lines = [header, "-" * len(header)]
    for r in rows:
        lines.append(" | ".join([
            r["material_prefix"] or "",
            r["status"] or "",
            r["plan_super"] or "",
            r["plan_model"] or "",
            r["proposed_plan_super"] or "",
            r["proposed_plan_model"] or "",
            r["bike_supers"] or "",
            r["bike_models"] or "",
        ]))

    plan_pairs = con.execute(
        "SELECT DISTINCT plan_super, plan_model FROM plan "
        "WHERE plan_model IS NOT NULL "
        "ORDER BY plan_super, plan_model"
    ).fetchall()
    lines.append("")
    lines.append(f"== plan ({len(plan_pairs)} distinct super/model pairs) ==")
    for p in plan_pairs:
        lines.append(f"  {p['plan_super'] or '(none)'} | {p['plan_model']}")

    return Response("\n".join(lines), mimetype="text/plain")


@app.route("/admin/mapping/reset", methods=["POST"])
@admin_required
def admin_mapping_reset():
    """Snapshot the current material_map to material_map_backups, wipe it,
    then re-seed with every unique material_prefix from orders as 'unmapped'
    and run auto-suggest. Designed for testing the auto-suggest pipeline."""
    con = db.get_conn()

    # Snapshot
    existing = con.execute(
        "SELECT material_prefix, plan_super, plan_model, status, notes, "
        "proposed_plan_super, proposed_plan_model, proposal_rejected "
        "FROM material_map ORDER BY material_prefix"
    ).fetchall()
    payload = json.dumps([dict(r) for r in existing])
    label = (request.form.get("label") or "").strip() or None
    con.execute(
        "INSERT INTO material_map_backups(label, row_count, payload) VALUES(?,?,?)",
        (label, len(existing), payload),
    )

    # Wipe
    con.execute("DELETE FROM material_map")

    # Re-seed from current orders so there's something to auto-suggest against
    cur = con.execute(
        "INSERT INTO material_map(material_prefix, status) "
        "SELECT DISTINCT material_prefix, 'unmapped' FROM orders "
        "WHERE material_prefix IS NOT NULL"
    )
    seeded = cur.rowcount

    proposed = _auto_suggest_mappings()
    con.commit()

    flash(
        f"Backed up {len(existing)} mappings, cleared the table, re-seeded "
        f"{seeded} prefix(es) from orders, and proposed {proposed} mapping(s).",
        "ok",
    )
    return redirect(url_for("admin_mapping"))


# ----- admin: embargoes -----------------------------------------------

@app.route("/admin/embargoes", methods=["GET"])
@admin_required
def admin_embargoes():
    con = db.get_conn()
    plan_models = con.execute(
        "SELECT DISTINCT plan_super, plan_model FROM plan "
        "WHERE plan_model IS NOT NULL "
        "ORDER BY plan_super, plan_model"
    ).fetchall()
    embargoes = {
        r["plan_model"]: r
        for r in con.execute("SELECT * FROM embargoes").fetchall()
    }
    return render_template("admin_embargoes.html",
                           plan_models=plan_models, embargoes=embargoes,
                           today=date.today().isoformat())


@app.route("/admin/embargoes/bulk", methods=["POST"])
@admin_required
def admin_embargoes_bulk():
    action = request.form.get("action")
    selected = request.form.getlist("selected")
    embargo_until = request.form.get("embargo_until", "").strip() or None

    if not selected:
        flash("Tick at least one model first.", "error")
        return redirect(url_for("admin_embargoes"))

    con = db.get_conn()
    if action == "set_date":
        if not embargo_until:
            flash("Pick a date before assigning.", "error")
            return redirect(url_for("admin_embargoes"))
        for pm in selected:
            con.execute(
                "INSERT INTO embargoes(plan_model, embargo_until, manually_hidden) "
                "VALUES(?, ?, 0) "
                "ON CONFLICT(plan_model) DO UPDATE SET "
                "  embargo_until=excluded.embargo_until, "
                "  manually_hidden=0, "
                "  updated_at=CURRENT_TIMESTAMP",
                (pm, embargo_until),
            )
        flash(f"Embargoed {len(selected)} model(s) until {embargo_until}.", "ok")
    elif action == "always_hide":
        for pm in selected:
            con.execute(
                "INSERT INTO embargoes(plan_model, embargo_until, manually_hidden) "
                "VALUES(?, NULL, 1) "
                "ON CONFLICT(plan_model) DO UPDATE SET "
                "  manually_hidden=1, "
                "  updated_at=CURRENT_TIMESTAMP",
                (pm,),
            )
        flash(f"{len(selected)} model(s) now hidden indefinitely.", "ok")
    elif action == "unembargo":
        placeholders = ",".join(["?"] * len(selected))
        con.execute(f"DELETE FROM embargoes WHERE plan_model IN ({placeholders})", selected)
        flash(f"Cleared embargo on {len(selected)} model(s).", "ok")
    else:
        flash("Unknown action.", "error")
    con.commit()
    return redirect(url_for("admin_embargoes"))


# ----- admin: dealers -------------------------------------------------

KNOWN_COUNTRIES = ["UK", "SWE", "NOR"]


@app.route("/admin/dealers", methods=["GET"])
@admin_required
def admin_dealers():
    con = db.get_conn()
    rows = con.execute("SELECT * FROM dealers ORDER BY country, name").fetchall()
    dealer_codes = con.execute(
        "SELECT DISTINCT dealer_code, dealer FROM orders "
        "WHERE dealer_code IS NOT NULL ORDER BY dealer"
    ).fetchall()
    admin_pw = db.get_setting("admin_password")
    months_visible = _dealer_months_visible()
    return render_template("admin_dealers.html", rows=rows, dealer_codes=dealer_codes,
                           countries=KNOWN_COUNTRIES, admin_pw=admin_pw,
                           months_visible=months_visible)


@app.route("/admin/dealers/save", methods=["POST"])
@admin_required
def admin_dealers_save():
    dealer_id = request.form.get("id", "").strip()
    password = request.form["password"].strip()
    name = request.form["name"].strip()
    country = request.form["country"].strip()
    dealer_code = request.form.get("dealer_code", "").strip() or None
    con = db.get_conn()
    try:
        if dealer_id:
            con.execute(
                "UPDATE dealers SET password=?, name=?, country=?, dealer_code=? WHERE id=?",
                (password, name, country, dealer_code, dealer_id),
            )
        else:
            con.execute(
                "INSERT INTO dealers(password,name,country,dealer_code) VALUES(?,?,?,?)",
                (password, name, country, dealer_code),
            )
        con.commit()
    except Exception as e:
        try:
            con.raw.rollback()
        except Exception:
            pass
        msg = str(e).lower()
        if "unique" in msg or "duplicate" in msg:
            flash("That password is already in use.", "error")
        else:
            flash(f"Couldn't save dealer: {e}", "error")
    return redirect(url_for("admin_dealers"))


@app.route("/admin/dealers/delete", methods=["POST"])
@admin_required
def admin_dealers_delete():
    db.get_conn().execute("DELETE FROM dealers WHERE id=?", (request.form["id"],))
    db.get_conn().commit()
    return redirect(url_for("admin_dealers"))


@app.route("/admin/dealers/admin_password", methods=["POST"])
@admin_required
def admin_set_admin_password():
    new_pw = request.form["admin_password"].strip()
    if not new_pw:
        flash("Admin password cannot be empty.", "error")
        return redirect(url_for("admin_dealers"))
    db.set_setting("admin_password", new_pw)
    flash("Admin password updated.", "ok")
    return redirect(url_for("admin_dealers"))


@app.route("/admin/dealers/months_visible", methods=["POST"])
@admin_required
def admin_set_months_visible():
    try:
        n = int(request.form.get("months_visible", "").strip())
    except ValueError:
        flash("Pick a number between 1 and 12.", "error")
        return redirect(url_for("admin_dealers"))
    if not 1 <= n <= 12:
        flash("Pick a number between 1 and 12.", "error")
        return redirect(url_for("admin_dealers"))
    db.set_setting("dealer_months_visible", str(n))
    flash(f"Dealers now see the next {n} month{'s' if n != 1 else ''}.", "ok")
    return redirect(url_for("admin_dealers"))


@app.route("/admin/accounting-month", methods=["POST"])
@admin_required
def admin_set_accounting_month():
    """Pin the accounting month manually, or reset to auto-detect. The window
    normally follows the allocation file's own dates; this is the override."""
    if request.form.get("reset"):
        db.set_setting("accounting_month", "")
        flash("Accounting month reset to auto-detect.", "ok")
        return redirect(url_for("admin_upload"))
    val = (request.form.get("accounting_month") or "").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})$", val)
    if not m or not 1 <= int(m.group(2)) <= 12:
        flash("Pick a valid month.", "error")
        return redirect(url_for("admin_upload"))
    normalized = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    db.set_setting("accounting_month", normalized)
    flash(f"Accounting month pinned to "
          f"{MONTH_NAMES[int(m.group(2)) - 1]} {m.group(1)}. "
          f"It stays pinned until you reset it or upload a new plan.", "ok")
    return redirect(url_for("admin_upload"))


# ----- admin: feedback inbox ------------------------------------------

@app.route("/admin/feedback")
@admin_required
def admin_feedback():
    show = request.args.get("show", "open")
    where = "WHERE resolved=0" if show != "all" else ""
    rows = db.get_conn().execute(
        f"SELECT id, submitted_at, submitter, page, message, resolved "
        f"FROM feedback {where} "
        f"ORDER BY id DESC"
    ).fetchall()
    open_count = db.get_conn().execute(
        "SELECT COUNT(*) AS c FROM feedback WHERE resolved=0"
    ).fetchone()["c"]
    return render_template(
        "admin_feedback.html",
        rows=rows,
        show=show,
        open_count=open_count,
    )


@app.route("/admin/feedback/resolve", methods=["POST"])
@admin_required
def admin_feedback_resolve():
    fid = request.form.get("id")
    resolved = 0 if request.form.get("reopen") else 1
    con = db.get_conn()
    con.execute("UPDATE feedback SET resolved=? WHERE id=?", (resolved, fid))
    con.commit()
    return redirect(url_for("admin_feedback", show=request.form.get("show") or "open"))


@app.route("/admin/feedback/delete", methods=["POST"])
@admin_required
def admin_feedback_delete():
    con = db.get_conn()
    con.execute("DELETE FROM feedback WHERE id=?", (request.form.get("id"),))
    con.commit()
    return redirect(url_for("admin_feedback", show=request.form.get("show") or "open"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5050"))
    app.run(host="127.0.0.1", port=port, debug=False)
