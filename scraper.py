import os
import json
import time
import re
import random
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

# ─── Config ──────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ["BOT_TOKEN"]
CHANNEL      = "@KupTaniej"
CHANNEL_MIN  = "@KupTaniejj"
AFFILIATE    = "kuptaniej04-21"
STATE_FILE   = "state.json"
MAX_PER_RUN  = int(os.environ.get("MAX_PER_RUN", "50"))
MAX_PER_CAT  = 3
EXPIRY_HOURS = 48
MIN_DISCOUNT = 10
MAX_DISCOUNT = 85

# ─── Categories: deals + bestsellers per department ──────────────────────────
def _deal_urls(dept):
    return [
        f"https://www.amazon.pl/s?i={dept}&rh=p_n_pct-off-with-tax%3A10-&page=1",
        f"https://www.amazon.pl/s?i={dept}&rh=p_n_pct-off-with-tax%3A10-&page=2",
    ]

def _best_urls(dept):
    return [
        f"https://www.amazon.pl/s?i={dept}&s=exact-aware-popularity-rank&page=1",
        f"https://www.amazon.pl/s?i={dept}&s=exact-aware-popularity-rank&page=2",
    ]

DEPARTMENTS = [
    "electronics", "mobile-phones", "computers", "kitchen", "beauty",
    "baby", "sporting", "stripbooks", "toys", "fashion",
    "grocery", "automotive", "hpc", "office-products",
]

DEPT_NAMES = {
    "electronics": "Elektronika", "mobile-phones": "Telefony",
    "computers": "Komputery", "kitchen": "Dom i kuchnia",
    "beauty": "Uroda", "baby": "Dzieci i niemowleta",
    "sporting": "Sport i outdoor", "stripbooks": "Ksiazki",
    "toys": "Zabawki", "fashion": "Moda",
    "grocery": "Zywnosc", "automotive": "Motoryzacja",
    "hpc": "Zdrowie", "office-products": "Artykuly biurowe",
}

# ─── State helpers ────────────────────────────────────────────────────────────

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
            return data.get("posted", {})
        except Exception:
            return {}

def save_state(posted: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"posted": posted}, f, ensure_ascii=False, indent=2)

def is_posted(asin: str, posted: dict) -> bool:
    if asin not in posted:
        return False
    ts = posted[asin]
    try:
        t = datetime.fromisoformat(ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - t < timedelta(hours=EXPIRY_HOURS)
    except Exception:
        return False

def mark_posted(asin: str):
    posted = load_state()
    posted[asin] = datetime.now(timezone.utc).isoformat()
    now = datetime.now(timezone.utc)
    clean = {k: v for k, v in posted.items()
             if _age(v, now) < EXPIRY_HOURS * 2}
    save_state(clean)

def _age(ts, now):
    try:
        t = datetime.fromisoformat(ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (now - t).total_seconds() / 3600
    except Exception:
        return 9999

# ─── Price parsing ───────────────────────────────────────────────────────────

def parse_price(text: str) -> float:
    text = (text
            .replace("PLN", "")
            .replace(u"z\u0142", "")
            .replace("zl", "")
            .replace(u"\xa0", "")
            .replace(" ", "")
            .strip())
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    text = re.sub(r"[^\d.]", "", text)
    try:
        return float(text)
    except ValueError:
        return 0.0

# ─── Scrape search page using Playwright ─────────────────────────────────────

def scrape_page(url: str, page, is_bestseller: bool = False) -> list:
    """Scrape a search results page.
    is_bestseller=True: accept items even without discounts.
    is_bestseller=False: only items with MIN_DISCOUNT+."""
    candidates = []
    retries = 2

    for attempt in range(retries + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(random.randint(2000, 4000))
            html = page.content()
            break
        except Exception as e:
            if attempt < retries:
                wait = (attempt + 1) * 3
                print(f"  [RETRY] {url} -- attempt {attempt+1} failed, waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"  [WARN] fetch failed {url}: {e}")
                return candidates

    soup = BeautifulSoup(html, "html.parser")

    for item in soup.select("[data-asin]"):
        asin = item.get("data-asin", "").strip()
        if not asin or len(asin) < 8:
            continue

        price_el = item.select_one(".a-price .a-offscreen")
        if not price_el:
            continue
        price = parse_price(price_el.get_text())
        if price <= 0:
            continue

        # --- Original price (struck-through, skip per-unit prices) ---
        orig_price = 0.0
        for el in item.select(".a-price.a-text-price .a-offscreen"):
            parent = el.parent
            grandparent = parent.parent if parent else None
            context = ""
            if parent:
                context += parent.get_text()
            if grandparent:
                context += grandparent.get_text()
            if "/" in context:
                continue
            v = parse_price(el.get_text())
            if v > price:
                orig_price = v
                break

        # --- Discount badge ---
        badge_pct = 0
        badge = item.select_one(".savingsPercentage, .a-badge-text")
        if badge:
            m = re.search(r"(\d+)\s*%", badge.get_text())
            if m:
                badge_pct = int(m.group(1))

        if orig_price <= price and badge_pct >= MIN_DISCOUNT:
            orig_price = round(price / (1 - badge_pct / 100), 2)

        # Calculate discount
        discount_pct = 0
        if orig_price > price:
            discount_pct = round((orig_price - price) / orig_price * 100)
            if discount_pct > MAX_DISCOUNT:
                discount_pct = 0
                orig_price = 0.0

        # For deal pages: require discount. For bestsellers: accept all.
        if not is_bestseller and discount_pct < MIN_DISCOUNT:
            continue

        # --- Rating (for sorting bestsellers) ---
        rating = 0.0
        rating_el = item.select_one(".a-icon-alt")
        if rating_el:
            m = re.search(r"([\d,\.]+)", rating_el.get_text())
            if m:
                rating = parse_price(m.group(1))

        reviews = 0
        reviews_el = item.select_one(".a-size-small .a-link-normal .a-size-base")
        if reviews_el:
            reviews = int(re.sub(r"[^\d]", "", reviews_el.get_text()) or 0)

        candidates.append({
            "asin":         asin,
            "price":        price,
            "orig_price":   orig_price,
            "discount_pct": discount_pct,
            "badge_pct":    badge_pct,
            "rating":       rating,
            "reviews":      reviews,
            "has_discount":  discount_pct >= MIN_DISCOUNT,
        })

    return candidates

# ─── Product page: title + discount + screenshot + product image ─────────────

def get_product_details(asin: str, page):
    url = (
        f"https://www.amazon.pl/dp/{asin}"
        f"?tag={AFFILIATE}&language=pl_PL"
    )
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(random.randint(2000, 3500))

        title = ""
        for sel in ["#productTitle", "#title span", "h1.a-size-large"]:
            el = page.query_selector(sel)
            if el:
                t = el.inner_text().strip()
                if t and len(t) > 5:
                    title = t
                    break

        if not title:
            print(f"  [SKIP] {asin} -- no title found")
            return None

        page_discount = None
        for sel in [".savingsPercentage", "span.a-color-price"]:
            el = page.query_selector(sel)
            if el:
                text = el.inner_text().strip()
                m = re.search(r"(\d+)\s*%", text)
                if m:
                    page_discount = int(m.group(1))
                    break

        page_orig_price = None
        for sel in [".basisPrice .a-offscreen", "span.a-text-price .a-offscreen"]:
            el = page.query_selector(sel)
            if el:
                text = el.inner_text()
                if "/" not in text:
                    v = parse_price(text)
                    if v > 0:
                        page_orig_price = v
                        break

        screenshot_path = f"/tmp/{asin}.png"
        page.screenshot(path=screenshot_path, full_page=False)

        product_image = None
        for sel in ["#landingImage", "#imgTagWrapperId img", "#main-image-container img", ".a-dynamic-image"]:
            el = page.query_selector(sel)
            if el:
                src = el.get_attribute("src")
                if src and "amazon" in src and not src.endswith(".gif"):
                    product_image = src
                    break

        product_image_path = None
        if product_image:
            try:
                resp = requests.get(product_image, timeout=15)
                if resp.status_code == 200:
                    product_image_path = f"/tmp/{asin}_product.jpg"
                    with open(product_image_path, "wb") as f:
                        f.write(resp.content)
            except Exception as e:
                print(f"  [WARN] image download failed: {e}")

        return {
            "title": title,
            "screenshot": screenshot_path,
            "product_image": product_image_path,
            "page_discount": page_discount,
            "page_orig_price": page_orig_price,
        }

    except Exception as e:
        print(f"  [WARN] product page error {asin}: {e}")
        return None

# ─── Telegram post: full (main channel) ──────────────────────────────────────

def send_telegram(asin, title, price, orig_price, discount_pct, screenshot):
    affiliate_url = f"https://www.amazon.pl/dp/{asin}?tag={AFFILIATE}"

    if discount_pct >= MIN_DISCOUNT and orig_price > price:
        caption = (
            f"\U0001f525 Znizka {discount_pct}%! \U0001f525\n"
            f"\n"
            f"\U0001f451 {title}\n"
            f"\n"
            f"\U0001f4b0 Cena: {price:,.2f} zl zamiast {orig_price:,.2f} zl\n"
            f"\n"
            f"\U0001f6d2 Kup teraz: {affiliate_url}"
        )
    else:
        caption = (
            f"\U0001f31f Bestseller \U0001f31f\n"
            f"\n"
            f"\U0001f451 {title}\n"
            f"\n"
            f"\U0001f4b0 Cena: {price:,.2f} zl\n"
            f"\n"
            f"\U0001f6d2 Kup teraz: {affiliate_url}"
        )

    api = f"https://api.telegram.org/bot{BOT_TOKEN}"

    try:
        with open(screenshot, "rb") as photo:
            resp = requests.post(
                f"{api}/sendPhoto",
                data={"chat_id": CHANNEL, "caption": caption},
                files={"photo": photo},
                timeout=30,
            )
        if resp.status_code == 200 and resp.json().get("ok"):
            print(f"  [OK] {asin} -- main channel sent")
            return True
        print(f"  [WARN] sendPhoto failed: {resp.text[:200]}")
    except Exception as e:
        print(f"  [WARN] sendPhoto exception: {e}")

    try:
        resp = requests.post(
            f"{api}/sendMessage",
            json={"chat_id": CHANNEL, "text": caption, "disable_web_page_preview": False},
            timeout=30,
        )
        if resp.status_code == 200 and resp.json().get("ok"):
            print(f"  [OK] {asin} -- main channel text-only")
            return True
        print(f"  [ERR] sendMessage failed: {resp.text[:200]}")
    except Exception as e:
        print(f"  [ERR] sendMessage exception: {e}")

    return False

# ─── Telegram post: minimal (second channel) ─────────────────────────────────

def send_telegram_minimal(asin, title, product_image_path):
    affiliate_url = f"https://www.amazon.pl/dp/{asin}?tag={AFFILIATE}"
    caption = f"{title}\n\n{affiliate_url}"

    api = f"https://api.telegram.org/bot{BOT_TOKEN}"

    if product_image_path and os.path.exists(product_image_path):
        try:
            with open(product_image_path, "rb") as photo:
                resp = requests.post(
                    f"{api}/sendPhoto",
                    data={"chat_id": CHANNEL_MIN, "caption": caption},
                    files={"photo": photo},
                    timeout=30,
                )
            if resp.status_code == 200 and resp.json().get("ok"):
                print(f"  [OK] {asin} -- minimal channel sent with image")
                return True
            print(f"  [WARN] minimal sendPhoto failed: {resp.text[:200]}")
        except Exception as e:
            print(f"  [WARN] minimal sendPhoto exception: {e}")

    try:
        resp = requests.post(
            f"{api}/sendMessage",
            json={"chat_id": CHANNEL_MIN, "text": caption, "disable_web_page_preview": False},
            timeout=30,
        )
        if resp.status_code == 200 and resp.json().get("ok"):
            print(f"  [OK] {asin} -- minimal channel text-only")
            return True
        print(f"  [ERR] minimal sendMessage failed: {resp.text[:200]}")
    except Exception as e:
        print(f"  [ERR] minimal sendMessage exception: {e}")

    return False

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"[START] {datetime.now(timezone.utc).isoformat()}  MAX_PER_RUN={MAX_PER_RUN}  MAX_PER_CAT={MAX_PER_CAT}")

    posted_snapshot = load_state()

    seen: set = set()
    candidates: list = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="pl-PL",
            extra_http_headers={"Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8"},
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        )
        page = ctx.new_page()

        # Phase 1: Scrape all categories
        for dept in DEPARTMENTS:
            cat_name = DEPT_NAMES[dept]
            print(f"[CAT] {cat_name}")

            # Deals (discount required)
            for url in _deal_urls(dept):
                items = scrape_page(url, page, is_bestseller=False)
                print(f"  deals -> {len(items)} hits")
                for item in items:
                    asin = item["asin"]
                    if asin in seen or is_posted(asin, posted_snapshot):
                        continue
                    seen.add(asin)
                    item["category"] = cat_name
                    item["source"] = "deal"
                    candidates.append(item)
                time.sleep(random.uniform(1.5, 3.0))

            # Bestsellers (no discount required)
            for url in _best_urls(dept):
                items = scrape_page(url, page, is_bestseller=True)
                print(f"  bestsellers -> {len(items)} hits")
                for item in items:
                    asin = item["asin"]
                    if asin in seen or is_posted(asin, posted_snapshot):
                        continue
                    seen.add(asin)
                    item["category"] = cat_name
                    item["source"] = "bestseller"
                    candidates.append(item)
                time.sleep(random.uniform(1.5, 3.0))

        # Sort: discounted items first (by discount%), then bestsellers (by reviews)
        candidates.sort(key=lambda x: (
            0 if x["has_discount"] else 1,   # discounted first
            -x["discount_pct"],               # highest discount first
            -x["reviews"],                    # most reviewed first
        ))

        print(f"[INFO] {len(candidates)} unique new candidates")
        discounted = sum(1 for c in candidates if c["has_discount"])
        print(f"  {discounted} with discount, {len(candidates) - discounted} bestsellers without discount")

        if not candidates:
            print("[DONE] Nothing new to post.")
            ctx.close()
            browser.close()
            return

        # Phase 2: Get product details and post
        posted_count = 0
        cat_counts = {}

        for c in candidates:
            if posted_count >= MAX_PER_RUN:
                break

            cat = c.get("category", "")
            if cat_counts.get(cat, 0) >= MAX_PER_CAT:
                continue

            asin         = c["asin"]
            price        = c["price"]
            orig_price   = c["orig_price"]
            discount_pct = c["discount_pct"]
            has_discount = c["has_discount"]

            if is_posted(asin, load_state()):
                print(f"  [SKIP] {asin} -- posted in a parallel check")
                continue

            tag = f"{discount_pct}% off" if has_discount else "bestseller"
            print(f"[ITEM] {asin}  {tag}  {price} zl  [{cat}]")

            details = get_product_details(asin, page)
            if not details:
                continue

            # Use product page discount if available (more accurate)
            final_discount = discount_pct
            final_orig     = orig_price

            if has_discount:
                if details["page_discount"] and MIN_DISCOUNT <= details["page_discount"] <= MAX_DISCOUNT:
                    final_discount = details["page_discount"]
                    final_orig = round(price / (1 - final_discount / 100), 2)
                    print(f"  Using page discount: {final_discount}% (was {discount_pct}%)")

                if details["page_orig_price"] and details["page_orig_price"] > price:
                    final_orig = details["page_orig_price"]
                    final_discount = round((final_orig - price) / final_orig * 100)
                    if final_discount > MAX_DISCOUNT:
                        final_discount = 0
                        final_orig = 0.0
                        has_discount = False
                    elif final_discount < MIN_DISCOUNT:
                        has_discount = False
                        final_discount = 0
                    print(f"  Using page orig price: {final_orig} -> {final_discount}%")

            ok = send_telegram(
                asin, details["title"],
                price, final_orig, final_discount if has_discount else 0,
                details["screenshot"],
            )

            if ok:
                send_telegram_minimal(asin, details["title"], details.get("product_image"))
                mark_posted(asin)
                posted_count += 1
                cat_counts[cat] = cat_counts.get(cat, 0) + 1
                print(f"  [{cat}: {cat_counts[cat]}/{MAX_PER_CAT}]")
                time.sleep(random.uniform(2, 4))

        ctx.close()
        browser.close()

    print(f"[DONE] Posted {posted_count} of {MAX_PER_RUN} allowed.")
    for cat, count in sorted(cat_counts.items()):
        print(f"  {cat}: {count}")


if __name__ == "__main__":
    main()
