"""
お得情報収集ボット メイン処理(オールインワン版)

このファイル1つに、収集・フィルタ・重複排除・通知・保存の
全ロジックをまとめています。理由: 今後の修正をスマホ(GitHubのbrowser上での
ファイル編集)だけで完結させたいため、ファイル数を極力減らしています。

普段いじるのは基本的に config.yaml だけで十分なはずです。
「新しい判定ロジックを追加したい」等、コード自体を直す場合だけ
このファイル内の該当セクションを編集してください
(見出しコメント "# ===== ○○ =====" で検索すると該当箇所に飛べます)。

全体の流れ:
1. config.yaml を読み込む
2. 各情報源を巡回して Deal のリストを作る
3. ルールベースでフィルタリング(ノイズ除去)
4. 通知済みID・タイトル類似度で重複を除外
5. Discord/LINEに通知
6. 通知済み情報とWeb一覧用データ(docs/deals.json)を保存
"""
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta, date
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
import xml.etree.ElementTree as ET

import requests
import yaml
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
DATA_PATH = BASE_DIR / "data" / "seen_items.json"
FEED_PATH = BASE_DIR / "docs" / "deals.json"

JST = timezone(timedelta(hours=9))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; OtokuInfoBot/1.0; personal use)"
}


# ============================================================
# ===== データ構造(Deal) =====
# ============================================================
@dataclass
class Deal:
    id: str            # 重複判定用の一意なID(通常はURL)
    title: str
    url: str
    price: int | None = None
    discount_percent: float | None = None
    discount_yen: int | None = None
    source: str = ""
    category: str = "その他"
    bypass_filter: bool = False  # Trueなら閾値判定をスキップして通す


# ============================================================
# ===== フィルタ(ノイズ除去のルール) =====
# ============================================================
def is_noteworthy(deal: Deal, config: dict) -> bool:
    f = config["filter"]
    title_lower = deal.title.lower()

    for kw in f.get("exclude_keywords", []):
        if kw.lower() in title_lower:
            return False

    if deal.bypass_filter:
        return True

    boosted = any(kw.lower() in title_lower for kw in f.get("boost_keywords", []))
    percent_threshold = f["min_discount_percent"] - (5 if boosted else 0)
    yen_threshold = f["min_discount_yen"]

    percent_ok = deal.discount_percent is not None and deal.discount_percent >= percent_threshold
    yen_ok = deal.discount_yen is not None and deal.discount_yen >= yen_threshold
    return percent_ok or yen_ok


# ============================================================
# ===== 終了済みキャンペーンの検知(タイトルの「○月○日まで」等から判定) =====
# ============================================================
# 年まで書かれているケース(例: 「2026年7月10日まで」)
END_DATE_RE_WITH_YEAR = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(?:\([^)]*\))?\s*(?:まで|迄)")
# 年なし・月日のみ(例: 「7月10日まで」)
END_DATE_RE_MD = re.compile(r"(\d{1,2})月(\d{1,2})日\s*(?:\([^)]*\))?\s*(?:まで|迄)")
# スラッシュ表記(例: 「7/10まで」)
END_DATE_RE_SLASH = re.compile(r"(\d{1,2})/(\d{1,2})\s*(?:まで|迄)")


def extract_end_date(title: str, reference_date: date) -> date | None:
    """タイトル文字列から終了日を推測する(見つからなければNone)"""
    m = END_DATE_RE_WITH_YEAR.search(title)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return date(y, mo, d)
        except ValueError:
            return None

    for pattern in (END_DATE_RE_MD, END_DATE_RE_SLASH):
        m = pattern.search(title)
        if not m:
            continue
        mo, d = map(int, m.groups())
        try:
            candidate = date(reference_date.year, mo, d)
        except ValueError:
            continue
        # 年をまたぐケースの対策: 明らかに大きく過去にずれる場合は来年の日付とみなす
        # (例: 12月に「1月10日まで」という記事が出た場合)
        if (reference_date - candidate).days > 200:
            try:
                candidate = date(reference_date.year + 1, mo, d)
            except ValueError:
                pass
        return candidate

    return None


def is_expired(title: str, reference_date: date) -> bool:
    """タイトルに書かれた終了日が今日より前なら「終了済み」とみなす"""
    end_date = extract_end_date(title, reference_date)
    if end_date is None:
        return False  # 終了日が読み取れない場合は除外しない(誤除外を避ける)
    return end_date < reference_date


# ============================================================
# ===== タイトル類似度による重複(名寄せ)判定 =====
# ============================================================
def normalize_title(title: str) -> str:
    text = title.lower()
    text = re.sub(r"[【】\[\]（）()「」『』!！?？・:：/／\-_~〜]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def is_similar_title(title_a: str, title_b: str, threshold: float = 0.6) -> bool:
    a, b = normalize_title(title_a), normalize_title(title_b)
    if not a or not b:
        return False
    return SequenceMatcher(None, a, b).ratio() >= threshold


def dedupe_by_title(deals: list, known_titles: list, threshold: float = 0.6):
    accepted, accepted_titles = [], []
    for deal in deals:
        is_dup = any(is_similar_title(deal.title, k, threshold) for k in known_titles)
        if not is_dup:
            is_dup = any(is_similar_title(deal.title, t, threshold) for t in accepted_titles)
        if not is_dup:
            accepted.append(deal)
            accepted_titles.append(deal.title)
    return accepted, accepted_titles


# ============================================================
# ===== 保存(通知済み管理 & Web一覧用データ) =====
# ============================================================
def _load_raw() -> dict:
    if not DATA_PATH.exists():
        return {}
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def load_seen_ids() -> set:
    return set(_load_raw().get("seen_ids", []))


def load_seen_titles(max_load: int = 300) -> list:
    return _load_raw().get("seen_titles", [])[-max_load:]


def save_seen(seen_ids: set, seen_titles: list, max_keep: int = 2000, max_keep_titles: int = 300) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "seen_ids": list(seen_ids)[-max_keep:],
                "seen_titles": list(seen_titles)[-max_keep_titles:],
            },
            f, ensure_ascii=False, indent=2,
        )


def append_to_feed(deals: list, max_keep: int = 100) -> None:
    """
    GitHub Pagesで表示する一覧(docs/deals.json)に新着を追記する。

    既存データも毎回「今のフィルタ・関連性判定」で再チェックし、古いロジックの
    時代に紛れ込んだ無関係な記事(音楽フェスの話題など)を自動的に取り除く。
    これにより、判定ロジックを直した後に手動でdeals.jsonをリセットしなくても
    次回の自動実行で自然に綺麗になる。
    """
    FEED_PATH.parent.mkdir(parents=True, exist_ok=True)

    existing = []
    if FEED_PATH.exists():
        with open(FEED_PATH, "r", encoding="utf-8") as f:
            try:
                existing = json.load(f).get("deals", [])
            except json.JSONDecodeError:
                existing = []

    # 既存データを今の関連性キーワード判定で再チェック(古い基準の残骸を除去)
    existing = [e for e in existing if is_relevant_news(e.get("title", "")) or e.get("category") != "セール速報"]

    now = datetime.now(JST).isoformat()
    new_entries = [
        {
            "title": d.title, "url": d.url, "source": d.source, "category": d.category,
            "discount_percent": d.discount_percent, "price": d.price, "detected_at": now,
        }
        for d in deals
    ]
    combined = (new_entries + existing)[:max_keep]
    # 検知日時(detected_at)が新しい順に並べ替える(サイトの巡回順ではなく日付順にするため)
    combined.sort(key=lambda x: x.get("detected_at", ""), reverse=True)
    with open(FEED_PATH, "w", encoding="utf-8") as f:
        json.dump({"updated_at": now, "deals": combined}, f, ensure_ascii=False, indent=2)


# ============================================================
# ===== 通知(Discord / LINE) =====
# ============================================================
def notify_discord(deal: Deal, webhook_url: str) -> None:
    discount_info = f"{deal.discount_percent:.0f}%OFF" if deal.discount_percent else ""
    price_info = f"{deal.price:,}円" if deal.price else "価格不明"
    content = (
        f"🔥 **お得情報を検知しました**\n"
        f"[{deal.category}] 【{deal.source}】{deal.title}\n"
        f"{discount_info} / {price_info}\n"
        f"{deal.url}"
    )
    resp = requests.post(webhook_url, json={"content": content}, timeout=10)
    resp.raise_for_status()


def notify_line(deal: Deal, channel_access_token: str, user_id: str) -> None:
    discount_info = f"{deal.discount_percent:.0f}%OFF" if deal.discount_percent else ""
    price_info = f"{deal.price:,}円" if deal.price else "価格不明"
    text = f"🔥お得情報\n【{deal.source}】{deal.title}\n{discount_info} / {price_info}\n{deal.url}"
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {channel_access_token}",
    }
    payload = {"to": user_id, "messages": [{"type": "text", "text": text}]}
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    resp.raise_for_status()


def send_notifications(deals: list, config: dict) -> None:
    import os
    notify_cfg = config.get("notify", {})
    discord_cfg = notify_cfg.get("discord", {})
    line_cfg = notify_cfg.get("line", {})

    discord_webhook = os.environ.get(discord_cfg.get("webhook_url_env", ""), "")
    line_token = os.environ.get(line_cfg.get("channel_access_token_env", ""), "")
    line_user_id = os.environ.get(line_cfg.get("user_id_env", ""), "")

    for deal in deals:
        if discord_cfg.get("enabled") and discord_webhook:
            try:
                notify_discord(deal, discord_webhook)
            except Exception as e:  # noqa: BLE001
                print(f"[Discord通知エラー] {deal.title}: {e}")
        if line_cfg.get("enabled") and line_token and line_user_id:
            try:
                notify_line(deal, line_token, line_user_id)
            except Exception as e:  # noqa: BLE001
                print(f"[LINE通知エラー] {deal.title}: {e}")


# ============================================================
# ===== スクレイパー: 価格.com 大幅値下がりランキング =====
# ============================================================
PERCENT_RE_KAKAKU = re.compile(r"(\d{1,3})\s*%\s*DOWN", re.IGNORECASE)
PRICE_RE_KAKAKU = re.compile(r"([\d,]{3,})\s*円")


def scrape_kakaku(url: str, source_name: str, category: str = "家電・通販") -> list[Deal]:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.encoding = resp.apparent_encoding
    soup = BeautifulSoup(resp.text, "html.parser")

    deals = []
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "/item/" not in href:
            continue

        container = link
        block_text = ""
        for _ in range(4):
            container = container.parent
            if container is None:
                break
            block_text = container.get_text(separator=" ", strip=True)
            if PERCENT_RE_KAKAKU.search(block_text) and PRICE_RE_KAKAKU.search(block_text):
                break

        percent_match = PERCENT_RE_KAKAKU.search(block_text)
        price_match = PRICE_RE_KAKAKU.search(block_text)
        if not percent_match:
            continue

        title = link.get_text(strip=True)
        if not title:
            continue

        full_url = href if href.startswith("http") else f"https://kakaku.com{href}"
        deals.append(Deal(
            id=full_url, title=title, url=full_url,
            price=int(price_match.group(1).replace(",", "")) if price_match else None,
            discount_percent=float(percent_match.group(1)),
            source=source_name, category=category,
        ))

    unique = {d.id: d for d in deals}
    return list(unique.values())


# ============================================================
# ===== スクレイパー: CSSセレクタ指定の汎用スクレイパー =====
# ============================================================
def scrape_generic_html(url: str, source_name: str, selectors: dict, category: str = "家電・通販") -> list[Deal]:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.encoding = resp.apparent_encoding
    soup = BeautifulSoup(resp.text, "html.parser")

    deals = []
    for item in soup.select(selectors.get("item", "")):
        title_el = item.select_one(selectors.get("title", ""))
        link_el = item.select_one(selectors.get("link", "a"))
        if not title_el or not link_el or not link_el.get("href"):
            continue

        href = link_el["href"]
        full_url = href if href.startswith("http") else requests.compat.urljoin(url, href)
        deals.append(Deal(
            id=full_url, title=title_el.get_text(strip=True), url=full_url,
            source=source_name, category=category,
        ))
    return deals


# ============================================================
# ===== スクレイパー: Googleニュース見出し監視 =====
# ============================================================
# タイトルにこれらの単語が1つも含まれない記事は、検索キーワードにはヒットしたものの
# セール・お得情報とは無関係な記事(音楽フェスの話題など)である可能性が高いため除外する
NEWS_RELEVANCE_KEYWORDS = [
    "セール", "割引", "off", "%", "還元", "開催決定", "クーポン", "特価",
    "タイムセール", "バーゲン", "値下げ", "円引き", "無料", "お得",
]


def is_relevant_news(title: str) -> bool:
    title_lower = title.lower()
    return any(kw.lower() in title_lower for kw in NEWS_RELEVANCE_KEYWORDS)


def scrape_news_watch(
    query: str, source_name: str, category: str = "セール速報",
    max_items: int = 3, max_age_days: int = 7,
) -> list[Deal]:
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=ja&gl=JP&ceid=JP:ja"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)

    deals = []
    for item in root.findall(".//item"):
        title_el, link_el = item.find("title"), item.find("link")
        if title_el is None or link_el is None:
            continue
        title, link = title_el.text or "", link_el.text or ""

        # セール・お得情報と無関係そうな記事は除外する
        if not is_relevant_news(title):
            continue

        # 公開日が古い記事(去年の同じセールの記事など)は除外する
        pub_date_el = item.find("pubDate")
        if pub_date_el is not None and pub_date_el.text:
            try:
                pub_date = parsedate_to_datetime(pub_date_el.text)
                if pub_date.tzinfo is None:
                    pub_date = pub_date.replace(tzinfo=timezone.utc)
                if pub_date < cutoff:
                    continue
            except (TypeError, ValueError):
                pass  # 日付が読めない場合は念のため除外せず通す

        deals.append(Deal(
            id=link, title=title, url=link,
            source=source_name, category=category, bypass_filter=True,
        ))
        if len(deals) >= max_items:
            break
    return deals


# ============================================================
# ===== スクレイパー: ポイント還元/割引率キャンペーン監視 =====
# ============================================================
POINT_CAMPAIGN_PATTERNS = {
    "point_back": re.compile(
        r"(?:最大\s*)?(\d{1,3})\s*%\s*(?:相当)?\s*(?:が)?\s*(?:還元|戻って|バック|上乗せ)"
    ),
    "discount": re.compile(
        r"(?:最大\s*)?(?:約\s*)?(\d{1,3})\s*%\s*(?:OFF|オフ)", re.IGNORECASE
    ),
}


def scrape_point_campaign(
    url: str, source_name: str, min_percent: int = 10,
    pattern: str = "point_back", category: str = "ポイント還元",
) -> list[Deal]:
    percent_re = POINT_CAMPAIGN_PATTERNS.get(pattern, POINT_CAMPAIGN_PATTERNS["point_back"])
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.encoding = resp.apparent_encoding
    soup = BeautifulSoup(resp.text, "html.parser")

    deals = []
    for link in soup.find_all("a", href=True):
        href = link["href"]
        container = link
        block_text = ""
        match = None
        for _ in range(3):
            container = container.parent
            if container is None:
                break
            block_text = container.get_text(separator=" ", strip=True)
            match = percent_re.search(block_text)
            if match:
                break

        if not match:
            match = percent_re.search(link.get_text(strip=True))
            block_text = link.get_text(strip=True)
        if not match:
            continue

        percent = float(match.group(1))
        if percent < min_percent:
            continue

        title = link.get_text(strip=True) or block_text[:40]
        full_url = href if href.startswith("http") else requests.compat.urljoin(url, href)
        label = "ポイント還元" if pattern == "point_back" else "割引"
        deals.append(Deal(
            id=f"{full_url}#{percent}%", title=f"{title}({percent:.0f}%{label})",
            url=full_url, discount_percent=percent, source=source_name, category=category,
        ))

    unique = {d.id: d for d in deals}
    return list(unique.values())


# ============================================================
# ===== スクレイパー: 公式RSS/Atomフィード監視 =====
# ============================================================
ATOM_NS = "{http://www.w3.org/2005/Atom}"


def _parse_rss(content: bytes, source_name: str, category: str, max_items: int) -> list[Deal]:
    """RSS/AtomのXML本文を解析してDealのリストにする(共通処理)"""
    root = ET.fromstring(content)

    entries = root.findall(".//item")
    is_atom = False
    if not entries:
        entries = root.findall(f".//{ATOM_NS}entry")
        is_atom = True

    deals = []
    for entry in entries[:max_items]:
        if is_atom:
            title_el, link_el = entry.find(f"{ATOM_NS}title"), entry.find(f"{ATOM_NS}link")
            title = title_el.text if title_el is not None else ""
            link = link_el.get("href") if link_el is not None else ""
        else:
            title_el, link_el = entry.find("title"), entry.find("link")
            title = title_el.text if title_el is not None else ""
            link = link_el.text if link_el is not None else ""

        if not title or not link:
            continue

        discount_percent = None
        match = POINT_CAMPAIGN_PATTERNS["discount"].search(title)
        if match:
            discount_percent = float(match.group(1))

        deals.append(Deal(
            id=link, title=title, url=link,
            discount_percent=discount_percent, source=source_name, category=category,
        ))
    return deals


def scrape_rss_watch(url: str, source_name: str, category: str = "家電・通販", max_items: int = 20) -> list[Deal]:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return _parse_rss(resp.content, source_name, category, max_items)


# ============================================================
# ===== スクレイパー: Twitter/X監視(Nitter経由・フォールバックあり) =====
# ============================================================
# Twitter/Xの公式APIは有料化されたため、Nitter(ログイン不要のミラー)のRSSを使う。
# Nitterの公開インスタンスは頻繁に落ちるため、複数を順に試して最初に成功した
# ものを使う。全部落ちていたら、その回はスキップ(エラーにせず空リストを返す)。
#
# config.yamlでの指定例:
#   - name: "ビックカメラ公式X"
#     type: "twitter_watch"
#     account: "biccamera_com"   # @は不要
#     category: "家電・通販"
#
# ※Nitterインスタンスは流動的なので、下のリストは動かなくなったら
#   最新の稼働インスタンス(status.d420.de 等で確認)に差し替えてください。
NITTER_INSTANCES = [
    "https://xcancel.com",
    "https://nitter.poast.org",
    "https://nitter.privacyredirect.com",
    "https://nitter.space",
    "https://nitter.tiekoetter.com",
]


def scrape_twitter_watch(account: str, source_name: str, category: str = "家電・通販", max_items: int = 10) -> list[Deal]:
    account = account.lstrip("@")
    last_error = None

    for base in NITTER_INSTANCES:
        rss_url = f"{base}/{account}/rss"
        try:
            resp = requests.get(rss_url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            deals = _parse_rss(resp.content, source_name, category, max_items)
            if deals:
                print(f"[{source_name}] Nitter({base})から{len(deals)}件取得")
                return deals
        except Exception as e:  # noqa: BLE001
            last_error = e
            continue  # このインスタンスは駄目だったので次を試す

    # 全インスタンスが駄目だった場合
    print(f"[{source_name}] 稼働中のNitterインスタンスが見つかりませんでした(最後のエラー: {last_error})")
    return []


# ============================================================
# ===== ウォッチリスト(ピンポイント商品の価格監視) =====
# ============================================================
def check_watchlist(watchlist: list) -> list[Deal]:
    deals = []
    for watch in watchlist:
        try:
            resp = requests.get(watch["url"], headers=HEADERS, timeout=20)
            resp.encoding = resp.apparent_encoding
            soup = BeautifulSoup(resp.text, "html.parser")
            text = soup.get_text(separator=" ", strip=True)
            match = PRICE_RE_KAKAKU.search(text)
            price = int(match.group(1).replace(",", "")) if match else None
        except Exception as e:  # noqa: BLE001
            print(f"[ウォッチリストエラー] {watch['name']}: {e}")
            continue

        if price is None:
            continue

        if price <= watch["target_price"]:
            deals.append(Deal(
                id=f"{watch['url']}#{price}",
                title=f"{watch['name']} が目標価格以下になりました",
                url=watch["url"], price=price, source="ウォッチリスト",
                category=watch.get("category", "家電・通販"), bypass_filter=True,
            ))
    return deals


# ============================================================
# ===== メイン処理 =====
# ============================================================
def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def collect_deals(config: dict) -> list:
    all_deals = []
    for source in config["sources"]:
        try:
            t = source["type"]
            if t == "kakaku_pricedown":
                deals = scrape_kakaku(source["url"], source["name"], source.get("category", "家電・通販"))
            elif t == "generic_html":
                deals = scrape_generic_html(
                    source["url"], source["name"], source.get("selectors", {}), source.get("category", "家電・通販")
                )
            elif t == "news_watch":
                deals = scrape_news_watch(
                    source["query"], source["name"], source.get("category", "セール速報"),
                    max_items=source.get("max_items", 3),
                    max_age_days=source.get("max_age_days", config.get("filter", {}).get("max_news_age_days", 7)),
                )
            elif t == "point_campaign":
                deals = scrape_point_campaign(
                    source["url"], source["name"], source.get("min_percent", 10),
                    source.get("pattern", "point_back"), source.get("category", "ポイント還元"),
                )
            elif t == "rss_watch":
                deals = scrape_rss_watch(source["url"], source["name"], source.get("category", "家電・通販"))
            elif t == "twitter_watch":
                deals = scrape_twitter_watch(source["account"], source["name"], source.get("category", "家電・通販"))
            else:
                print(f"[警告] 未対応のtype: {t}")
                continue

            print(f"[{source['name']}] {len(deals)}件取得")
            all_deals.extend(deals)
        except Exception as e:  # noqa: BLE001
            print(f"[エラー] {source['name']} の取得に失敗: {e}")

    return all_deals


# ============================================================
# ===== AI精査(Gemini API・任意) =====
# ============================================================
# 環境変数 GEMINI_API_KEY が設定されている場合のみ動作する。
# 未設定なら、この処理はまるごとスキップされ、従来のルールベースだけで動く。
#
# 1回のAPI呼び出しで「無関係な情報の除外」「同じ話題のまとめ」「一行要約」を
# まとめて行う。無料枠(Gemini Flash: 1日1500リクエスト)に収めるため、
# ルールベースで絞り込んだ後の候補だけをまとめて渡す。
GEMINI_MODEL = "gemini-2.5-flash"


def refine_with_ai(deals: list) -> list:
    import os
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("[AI精査] GEMINI_API_KEYが未設定のためスキップ(従来のルールベースで動作)")
        return deals
    if not deals:
        print("[AI精査] 対象0件のためスキップ")
        return deals

    print(f"[AI精査] {len(deals)}件をAIに送信します...")

    # AIに渡す候補一覧を作る(番号付き)
    items_text = "\n".join(
        f"{i}. [{d.category}] {d.title}" for i, d in enumerate(deals)
    )

    prompt = (
        "あなたは日本のお得情報・セール情報をまとめるキュレーターです。"
        "以下は自動収集したセール情報の候補リストです。次の3つを行ってください。\n"
        "1. セール・値下げ・還元と無関係な記事(音楽フェス、解説記事、"
        "「いつ安くなる?」等の攻略記事、単なる予想記事)を除外する。\n"
        "2. 同じセール・キャンペーンを指す複数の記事は、最も情報量の多い1件だけ残す。\n"
        "3. 残した各項目に、20文字以内の分かりやすい一行要約をつける。\n\n"
        "必ず以下のJSON形式のみで出力してください(前後に説明文やマークダウンは不要):\n"
        '{"keep": [{"index": 元の番号, "summary": "一行要約"}, ...]}\n\n'
        f"候補リスト:\n{items_text}"
    )

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }

    try:
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        result = json.loads(text)
    except Exception as e:  # noqa: BLE001
        print(f"[AI精査エラー] スキップして従来処理を続行: {e}")
        return deals

    kept = []
    for entry in result.get("keep", []):
        idx = entry.get("index")
        if idx is None or not (0 <= idx < len(deals)):
            continue
        deal = deals[idx]
        summary = entry.get("summary", "").strip()
        if summary:
            # 要約をタイトルの先頭に付与(元タイトルも残す)
            deal.title = f"{summary} ｜ {deal.title}"
        kept.append(deal)

    print(f"[AI精査] {len(deals)}件 → {len(kept)}件に整理")
    return kept


def main():
    config = load_config()
    seen_ids = load_seen_ids()
    seen_titles = load_seen_titles()

    all_deals = collect_deals(config)

    if config.get("watchlist"):
        watch_deals = check_watchlist(config["watchlist"])
        print(f"[ウォッチリスト] {len(watch_deals)}件が目標価格以下")
        all_deals.extend(watch_deals)

    noteworthy = [d for d in all_deals if is_noteworthy(d, config)]
    print(f"フィルタ後の該当件数: {len(noteworthy)}")

    today = datetime.now(JST).date()
    active = [d for d in noteworthy if not is_expired(d.title, today)]
    print(f"終了済み除外後の件数: {len(active)}")

    new_deals = [d for d in active if d.id not in seen_ids]
    print(f"未通知の新着件数(ID基準): {len(new_deals)}")

    deduped_deals, newly_seen_titles = dedupe_by_title(new_deals, seen_titles)
    print(f"名寄せ後の新着件数: {len(deduped_deals)}")

    # AI精査(GEMINI_API_KEYがある時だけ動く。無ければそのまま素通り)
    deduped_deals = refine_with_ai(deduped_deals)

    if deduped_deals:
        send_notifications(deduped_deals, config)
        seen_ids.update(d.id for d in deduped_deals)
        seen_titles.extend(newly_seen_titles)
        save_seen(seen_ids, seen_titles)
        append_to_feed(deduped_deals)
    else:
        print("新着の該当なし")


if __name__ == "__main__":
    main()
