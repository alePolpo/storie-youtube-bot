#!/usr/bin/env python3
"""
Storie YouTube -> Telegram

Mandi al bot un link YouTube (anche condiviso dall'app) e ti risponde con:
  1. la storia Instagram 1080x1920: copertina del video in una card, sfondo
     sfocato coi colori della copertina, logo YouTube sotto
  2. il link da mettere nello sticker: apre il video fuori da Instagram
     (app YouTube o browser), passando dal worker in worker/

Python 3 + Pillow. Risponde solo a TELEGRAM_CHAT_ID.

Uso:
  python3 bot.py                  avvia il bot
  python3 bot.py --chat-id        mostra il tuo chat id (dopo aver scritto al bot)
  python3 bot.py --test <link>    salva la storia in storia.png, senza Telegram
"""
import html
import io
import math
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

BASE = Path(__file__).resolve().parent


def load_env():
    p = BASE / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
LINK_BASE = os.environ.get("LINK_BASE", "").rstrip("/")  # es. https://guarda.<sottodominio>.workers.dev
AFFILIATE_TAG = os.environ.get("AFFILIATE_TAG", "")
API = "https://api.telegram.org"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
FONTS = BASE / "fonts"


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
        "Cookie": "SOCS=CAI; CONSENT=YES+1",  # salta la pagina dei cookie in UE
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ------------------------------------------------------------------- Amazon
# Stessa logica del worker amazon-affiliato: link pulito /dp/ASIN con il tag, o solo il tag.
AMZ_RE = re.compile(r"https?://(?:[\w-]+\.)?(?:amazon\.[a-z.]+|amzn\.(?:to|eu|com)|a\.co|link\.amazon)/[^\s<>\"')]*", re.I)
ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d|d)/([A-Z0-9]{10})", re.I)


# Da GitHub Actions Amazon non dà le pagine e link.amazon porta ad amazon.com:
# se c'è AMZ_PROXY si passa dal worker amazon-affiliato, che gira a Milano.
AMZ_PROXY = os.environ.get("AMZ_PROXY", "")
AMZ_PROXY_KEY = os.environ.get("AMZ_PROXY_KEY", "")


def amz_get(url, timeout=25):
    """(url finale dopo i redirect, contenuto)."""
    if AMZ_PROXY:
        req = urllib.request.Request(f"{AMZ_PROXY}/amz?u={urllib.parse.quote(url, safe='')}",
                                     headers={"X-Key": AMZ_PROXY_KEY, "User-Agent": UA})
    else:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "it-IT,it;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.headers.get("X-Url") or r.geturl(), r.read()


def affilia(url, final=None):
    if not AFFILIATE_TAG:
        raise RuntimeError("Manca AFFILIATE_TAG")
    url = final or url.rstrip(".,;:!?")
    if not re.search(r"(^|\.)amazon\.", urllib.parse.urlparse(url).hostname or "", re.I):  # link corto: segui il redirect
        url = amz_get(url)[0]
    u = urllib.parse.urlparse(url)
    if m := ASIN_RE.search(u.path):
        return f"https://{u.hostname}/dp/{m[1].upper()}?tag={AFFILIATE_TAG}"
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query) if k != "tag"] + [("tag", AFFILIATE_TAG)]
    return urllib.parse.urlunparse(u._replace(query=urllib.parse.urlencode(q)))


def _block(h, name):
    """HTML di un widget della pagina prodotto, senza style/script."""
    i = h.find(f'id="{name}"')
    if i < 0:
        return ""
    j = h.find('feature_div" class="celwidget"', i + 50)
    b = re.sub(r"<(style|script).*?</\1>", "", h[i:j if j > 0 else i + 15000], flags=re.S)
    return b[b.find(">") + 1:]  # via il resto del tag di apertura (id=..., class=...)


def _text(html_):
    return [t.strip() for t in re.split(r"<[^>]+>", html_) if t.strip()]


def amazon_info(url, page=None):
    """Titolo, foto, prezzo e badge dalla pagina prodotto; ogni pezzo c'è solo se la pagina lo mostra."""
    u = urllib.parse.urlparse(url)
    m = ASIN_RE.search(u.path)
    if not m:
        return None
    if page is None:
        page = amz_get(f"https://{u.hostname}/dp/{m[1].upper()}")[1]
    page = page.decode("utf-8", "replace")
    if re.search(r"captcha|Digita i caratteri", page, re.I) and 'id="productTitle"' not in page:
        raise RuntimeError("Amazon ha chiesto un captcha")
    t = re.search(r'id="productTitle"[^>]*>\s*([^<]+)', page)
    img = re.findall(r'"hiRes":"([^"]+)"', page) or re.findall(r'"large":"([^"]+)"', page)
    if not t or not img:
        raise RuntimeError("Pagina prodotto non leggibile")
    info = {"title": re.sub(r"[\u200b-\u200f\ufeff]", "", html.unescape(re.sub(r"\s+", " ", t[1]))).strip(), "image": amz_get(img[0])[1],
            "price": None, "old": None, "pct": None, "deal": None, "deal_bg": "#CC0C39",
            "rating": None, "count": None, "choice": False}
    core = _block(page, "corePriceDisplay_desktop_feature_div")
    if p := re.search(r'priceToPay.*?a-price-whole">([\d.]+)<span class="a-price-decimal">,</span></span>'
                      r'<span class="a-price-fraction">(\d+)', core, re.S):
        info["price"] = (p[1], p[2])
        if o := re.search(r'apex-basisprice-value.*?a-offscreen">([\d.,]+)', core, re.S):
            info["old"] = o[1] + " €"
        if c := re.search(r'savingsPercentage[^>]*>\s*-?(\d+)%', core):
            info["pct"] = int(c[1])
    deal = _block(page, "dealBadge_feature_div")
    if texts := _text(deal):
        label = texts[0].split("NO_OF")[0].strip()
        if label and "{" not in label and "<" not in label:
            info["deal"] = label
            if b := re.search(r"--deal-badge-bg:\s*(#[0-9A-Fa-f]{6})", deal):
                info["deal_bg"] = b[1]
    if r := re.search(r'id="acrPopover"[^>]*title="([\d,]+) su 5', page):
        info["rating"] = float(r[1].replace(",", "."))
        if n := re.search(r'id="acrCustomerReviewText"[^>]*>\s*\(?([\d.]+)', page):
            info["count"] = n[1]
    info["choice"] = any(re.fullmatch(r"Scelta\s+Amazon", t) for t in _text(_block(page, "acBadge_feature_div")))
    return info


def _stars(img, x, y, size, rating):
    S = 4
    mask = Image.new("L", (size * 5 * S, size * S), 0)
    md = ImageDraw.Draw(mask)
    for i in range(5):
        cx, cy, r = (i + .5) * size * S, size * S / 2, size * S / 2
        md.polygon([(cx + (r if k % 2 == 0 else r * .42) * math.cos(-math.pi / 2 + k * math.pi / 5),
                     cy + (r if k % 2 == 0 else r * .42) * math.sin(-math.pi / 2 + k * math.pi / 5))
                    for k in range(10)], fill=255)
    mask = mask.resize((size * 5, size), Image.LANCZOS)
    fill = Image.new("L", mask.size, 0)
    ImageDraw.Draw(fill).rectangle([0, 0, int(size * rating), size], fill=255)
    img.paste(Image.composite(Image.new("RGB", mask.size, (255, 164, 28)),
                              Image.new("RGB", mask.size, (221, 221, 221)), fill), (x, y), mask)


def render_amazon(info):
    """Pagina prodotto semplificata come nell'app Amazon, larga 1080 e alta quanto il contenuto: ogni riga solo se il dato c'è."""
    pad = 48
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    src = Image.open(io.BytesIO(info["image"])).convert("RGB")
    src = src.crop(_bbox(src) or (0, 0, *src.size))  # via il bianco attorno al prodotto
    photo = ImageOps.contain(src, (W - 2 * pad, 760), Image.LANCZOS)
    f_t, f_r, f_b = font("Inter-Regular.ttf", 38), font("Inter-Regular.ttf", 36), font("Inter-Medium.ttf", 34)
    lines = wrap(info["title"], f_t, W - 2 * pad, 4)
    # altezza del blocco, per centrarlo in verticale
    h = len(lines) * 52 + 14 + (62 if info["rating"] else 0) + (90 if info["choice"] else 0) + photo.height + 30
    if info["deal"]:
        h += 84
    if info["price"]:
        h += 118 + (60 if info["old"] else 0)
    y = max(150, (H - h) // 2 - 20)
    for l in lines:
        d.text((pad, y), l, font=f_t, fill=(86, 89, 89)); y += 52
    y += 14
    if info["rating"]:
        txt = f"{info['rating']:.1f}".replace(".", ",")
        d.text((pad, y), txt, font=f_r, fill=(15, 17, 17))
        sx = pad + int(f_r.getlength(txt)) + 10
        _stars(img, sx, y + 4, 36, info["rating"])
        if info["count"]:
            d.text((sx + 5 * 36 + 14, y), f"({info['count']})", font=f_r, fill=(0, 113, 133))
        y += 62
    if info["choice"]:
        bw = int(f_b.getlength("Scelta Amazon")) + 40
        d.rounded_rectangle([pad, y, pad + bw, y + 58], 8, fill=(35, 47, 62))
        d.text((pad + 20, y + 29), "Scelta Amazon", font=f_b, fill="white", anchor="lm")
        y += 90
    img.paste(photo, ((W - photo.width) // 2, y)); y += photo.height + 30
    if info["deal"]:
        f_d = font("InterDisplay-SemiBold.ttf", 36)
        dw = int(f_d.getlength(info["deal"])) + 44
        d.rounded_rectangle([pad, y, pad + dw, y + 62], 6, fill=info["deal_bg"])
        d.text((pad + 22, y + 31), info["deal"], font=f_d, fill="white", anchor="lm"); y += 84
    if info["price"]:
        euros, cents = info["price"]
        x = pad
        if info["pct"]:
            f_pc = font("Inter-Regular.ttf", 76)
            d.text((x, y + 70), f"-{info['pct']}%", font=f_pc, fill=(204, 12, 57), anchor="ls")
            x += int(f_pc.getlength(f"-{info['pct']}%")) + 24
        f_big, f_sup = font("Inter-Regular.ttf", 112), font("Inter-Regular.ttf", 48)
        d.text((x, y + 110), euros, font=f_big, fill=(15, 17, 17), anchor="ls")
        x += int(f_big.getlength(euros)) + 4
        d.text((x, y + 40), f"{cents} €", font=f_sup, fill=(15, 17, 17), anchor="ls")
        if info["old"]:
            f_o, lab = font("Inter-Regular.ttf", 36), "Prezzo consigliato: "
            d.text((pad, y + 168), lab, font=f_o, fill=(86, 89, 89), anchor="ls")
            ox = pad + f_o.getlength(lab)
            d.text((ox, y + 168), info["old"], font=f_o, fill=(86, 89, 89), anchor="ls")
            d.line([(ox, y + 156), (ox + f_o.getlength(info["old"]), y + 156)], fill=(86, 89, 89), width=3)
    bb = _bbox(img)  # niente fasce bianche sopra e sotto
    img = img.crop((0, max(0, bb[1] - pad), W, min(H, bb[3] + pad)))
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def _bbox(img):
    """Riquadro di quello che non è bianco."""
    return ImageOps.invert(img.convert("L")).point(lambda v: 255 if v > 10 else 0).getbbox()


# ---------------------------------------------------------- /storia Amazon
GOTHAM = ("Gotham-Black.otf", "Gotham-Bold.otf", "Gotham-Medium.otf")
BLU, BLU_ACCESO = (18, 44, 137), (27, 80, 220)  # colori del canale
CAMPI_RE = re.compile(r"^\s*(nome|prezzo|prima|etichetta|nota)\s*:\s*(.+?)\s*$", re.I | re.M)


def gotham(name, size):
    """Gotham dal Mac o dal worker (non sta nel repo pubblico); se manca, Inter."""
    p = FONTS / name
    if not p.exists():
        try:
            p.write_bytes(_scarica_font(name))
        except Exception as e:  # noqa: BLE001
            log("Gotham non disponibile:", e)
            return font("InterDisplay-Bold.ttf", size)
    return ImageFont.truetype(str(p), size)


def _scarica_font(name):
    mac = Path.home() / "Library/Fonts" / name
    if mac.exists():
        return mac.read_bytes()
    req = urllib.request.Request(f"{AMZ_PROXY}/font/{name}", headers={"X-Key": AMZ_PROXY_KEY, "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def nome_breve(title):
    """'Apple AirPods Pro 3, Cancellazione attiva…' -> 'Apple AirPods Pro 3'."""
    t = re.split(r"\s*[,|–—(]\s*|\s+-\s+", title)[0].strip()
    return t if len(t) <= 60 else t[:60].rsplit(" ", 1)[0] + "…"


def euro(txt):
    """'249,00 €' / '249' / ('189','00') -> '249 €' (centesimi solo se ci sono)."""
    if isinstance(txt, tuple):
        txt = f"{txt[0]},{txt[1]}"
    t = re.sub(r"[€\s]", "", str(txt)).removesuffix(",00")
    return f"{t} €"


def etichetta(info):
    d = info.get("deal") or ""
    for ev in ("Black Friday", "Cyber Monday", "Prime Day"):
        if ev.lower() in d.lower():
            return ev.upper()
    return d.upper() or ("OFFERTA" if info.get("pct") else "")


def tag(text, f, fg, bg, angle=0.0, padx=28, pady=18):
    """Scritta su rettangolo pieno, eventualmente un po' storta (come gli adesivi)."""
    l, t, r_, b = f.getbbox(text)
    w, h = r_ - l + 2 * padx, b - t + 2 * pady
    im = Image.new("RGBA", (w, h), bg)
    ImageDraw.Draw(im).text((padx - l, pady - t), text, font=f, fill=fg)
    return im.rotate(angle, Image.BICUBIC, expand=True) if angle else im


def render_storia(info, campi=None):
    """Storia 1080x1920 per un'offerta: sfondo sfocato, etichetta in alto, card con foto, nome e prezzi.
    campi (dal messaggio, facoltativi): nome, prezzo, prima, etichetta, nota."""
    c = {k.lower(): v for k, v in (campi or {}).items()}
    nome = c.get("nome") or nome_breve(info["title"])
    prezzo = euro(c["prezzo"]) if "prezzo" in c else (euro(info["price"]) if info.get("price") else "")
    prima = euro(c["prima"]) if "prima" in c else (euro(info["old"]) if info.get("old") else "")
    label = (c.get("etichetta") or etichetta(info)).upper()

    src = Image.open(io.BytesIO(info["image"])).convert("RGB")
    src = src.crop(_bbox(src) or (0, 0, *src.size))
    canvas = background(src).convert("RGBA")
    d = ImageDraw.Draw(canvas)

    y = 250  # sotto la barra del profilo di Instagram
    if label:
        t = tag(label, gotham("Gotham-Black.otf", 64), "white", (15, 15, 15), angle=2)
        canvas.alpha_composite(t, ((W - t.width) // 2, y)); y += t.height + 18
    f_aff = gotham("Gotham-Medium.otf", 30)
    a = tag("LINK AFFILIATO", f_aff, (15, 15, 15), "white", padx=18, pady=10)  # leggibile su ogni sfondo
    canvas.alpha_composite(a, ((W - a.width) // 2, y)); y += a.height + 44

    # card bianca: foto, nome, prezzi
    pad, cw = 60, CARD_W
    f_nome, f_prezzo, f_prima = gotham("Gotham-Bold.otf", 54), gotham("Gotham-Black.otf", 120), gotham("Gotham-Medium.otf", 50)
    righe = wrap(nome.upper(), f_nome, cw - 2 * pad, 2)
    price_h = 170 if prezzo else 0
    foto_max = 1920 - 360 - y - (pad + len(righe) * 64 + 30 + price_h + pad)  # 360 liberi per il link di IG
    photo = ImageOps.contain(src, (cw - 2 * pad, min(640, foto_max)), Image.LANCZOS)
    ch = pad + photo.height + 40 + len(righe) * 64 + (30 + price_h if prezzo else 0) + pad
    card = Image.new("RGB", (cw, ch), "white")
    card.paste(photo, ((cw - photo.width) // 2, pad))
    cd = ImageDraw.Draw(card)
    cy = pad + photo.height + 40
    for riga in righe:
        cd.text((pad, cy), riga, font=f_nome, fill=(15, 15, 15)); cy += 64
    if prezzo:
        cy += 30
        box = tag(prezzo, f_prezzo, "white", BLU_ACCESO, padx=30, pady=24)
        card.paste(box, (pad, cy), box)
        if prima:
            px = pad + box.width + 34
            cd.text((px, cy + box.height // 2 - 26), prima, font=f_prima, fill=(110, 110, 110), anchor="lm")
            pw = f_prima.getlength(prima)
            cd.line([(px, cy + box.height // 2 - 26), (px + pw, cy + box.height // 2 - 26)], fill=(110, 110, 110), width=5)
            if info.get("pct") and "prezzo" not in c:
                cd.text((px, cy + box.height // 2 + 34), f"-{info['pct']}%", font=f_prima, fill=BLU_ACCESO, anchor="lm")

    cx = CARD_X
    sh = Image.new("L", (W, H), 0)  # ombra morbida come nelle storie YouTube
    sh.paste(mask_rounded((cw, ch), RADIUS), (cx, y + 14))
    sh = sh.filter(ImageFilter.GaussianBlur(28)).point(lambda v: v * 40 // 100)
    canvas.paste((0, 0, 0, 255), (0, 0, W, H), sh)
    canvas.paste(card, (cx, y), mask_rounded((cw, ch), RADIUS))
    y += ch + 30
    if c.get("nota"):
        n = tag(c["nota"].upper(), gotham("Gotham-Black.otf", 44), "white", (15, 15, 15), angle=-2)
        canvas.alpha_composite(n, (cx, y))

    out = io.BytesIO()
    canvas.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


# ------------------------------------------------------------------ YouTube
ID_RE = re.compile(r'(?:youtu\.be/|youtube\.com/(?:watch\?(?:\S*?&)?v=|shorts/|live/|embed/))'
                   r'([A-Za-z0-9_-]{11})')


def video_id(text):
    m = ID_RE.search(text or "")
    return m.group(1) if m else None


def fetch_info(vid):
    """Titolo e canale da oEmbed; durata e avatar dalla pagina del video (se c'è)."""
    url = f"https://www.youtube.com/watch?v={vid}"
    o = json.loads(http_get("https://www.youtube.com/oembed?format=json&url="
                            + urllib.parse.quote(url)))
    info = {"id": vid, "title": o["title"], "channel": o["author_name"],
            "duration": None, "avatar": None}
    try:
        page = http_get(url).decode("utf-8", "replace")
        if m := re.search(r'"lengthSeconds":"(\d+)"', page):
            info["duration"] = int(m.group(1))
        if m := re.search(r'"videoOwnerRenderer":\{"thumbnail":\{"thumbnails":\[\{"url":"([^"]+)"', page):
            info["avatar"] = re.sub(r"=s\d+-", "=s240-", m.group(1))
    except Exception as e:  # noqa: BLE001
        log("pagina video non leggibile, niente durata/avatar:", e)
    for name in ("maxresdefault", "sddefault", "hqdefault"):
        try:
            info["thumb"] = http_get(f"https://i.ytimg.com/vi/{vid}/{name}.jpg")
            break
        except urllib.error.HTTPError:
            continue
    return info


def fmt_duration(s):
    h, m, s = s // 3600, s // 60 % 60, s % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


# ------------------------------------------------------------------- render
W, H = 1080, 1920
CARD_X, CARD_W = 88, 904
THUMB_H = CARD_W * 9 // 16
RADIUS = 28
SS = 4  # supersampling delle maschere, Pillow non fa l'antialias sulle forme
EMOJI_RE = re.compile("[\U0001F000-\U0001FFFF☀-➿️‍]")


def font(name, size):
    return ImageFont.truetype(str(FONTS / name), size)


def mask_rounded(size, r, top_only=False):
    w, h = size
    m = Image.new("L", (w * SS, h * SS), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle((0, 0, w * SS - 1, h * SS - 1), r * SS, fill=255,
                        corners=(True, True, False, False) if top_only else None)
    return m.resize(size, Image.LANCZOS)


def mask_circle(d):
    m = Image.new("L", (d * SS, d * SS), 0)
    ImageDraw.Draw(m).ellipse((0, 0, d * SS - 1, d * SS - 1), fill=255)
    return m.resize((d, d), Image.LANCZOS)


def wrap(text, f, width, max_lines):
    words, lines = text.split(), []
    while words and len(lines) < max_lines:
        line = words.pop(0)
        while words and f.getlength(line + " " + words[0]) <= width:
            line += " " + words.pop(0)
        lines.append(line)
    if words:  # avanza testo: ellissi sull'ultima riga
        last = lines[-1]
        while f.getlength(last + "…") > width and " " in last:
            last = last.rsplit(" ", 1)[0]
        lines[-1] = last + "…"
    return lines


def thumb_16x9(data):
    im = Image.open(io.BytesIO(data)).convert("RGB")
    return ImageOps.fit(im, (1280, 720), Image.LANCZOS)  # toglie le bande di hqdefault


def background(thumb):
    # copertina schiacciata a pochi pixel e riallargata: restano solo le macchie di colore
    small = thumb.resize((5, 9), Image.BOX)
    small = ImageEnhance.Color(small).enhance(1.8)
    small = small.point(lambda v: 50 + v * 205 // 255)  # neri sollevati: niente angoli bui
    return small.resize((W, H), Image.BICUBIC).filter(ImageFilter.GaussianBlur(90))


def render(info):
    thumb = thumb_16x9(info["thumb"])
    canvas = background(thumb)

    f_title = font("InterDisplay-SemiBold.ttf", 46)
    f_chan = font("Inter-Regular.ttf", 34)
    pad, av = 42, 116
    text_x = pad + av + 34
    text_w = CARD_W - text_x - pad
    title = EMOJI_RE.sub("", info["title"]).strip()  # ponytail: Inter non ha emoji, si tolgono
    lines = wrap(title, f_title, text_w, 3)
    title_lh, chan_lh = 60, 50
    panel_h = max(pad + len(lines) * title_lh + 10 + chan_lh + pad, pad + av + pad)
    card_h = THUMB_H + panel_h

    logo_gap, logo_h = 48, 50
    block = card_h + logo_gap + logo_h
    card_y = (H - block) // 2 - 20

    # ombra morbida sotto la card
    sh = Image.new("L", (W, H), 0)
    sh.paste(mask_rounded((CARD_W, card_h), RADIUS), (CARD_X, card_y + 14))
    sh = sh.filter(ImageFilter.GaussianBlur(28)).point(lambda v: v * 45 // 100)
    canvas.paste(Image.new("RGB", (W, H), (0, 0, 0)), (0, 0), sh)

    card = Image.new("RGB", (CARD_W, card_h), "white")
    card.paste(thumb.resize((CARD_W, THUMB_H), Image.LANCZOS), (0, 0))
    d = ImageDraw.Draw(card)

    if info["duration"]:
        f_dur = font("Inter-Medium.ttf", 32)
        txt = fmt_duration(info["duration"])
        tw = f_dur.getlength(txt)
        bw, bh = int(tw + 40), 64
        bx, by = CARD_W - 32 - bw, THUMB_H - 36 - bh
        badge = Image.new("RGB", (bw, bh), (24, 24, 24))
        card.paste(badge, (bx, by), mask_rounded((bw, bh), 12))
        d.text((bx + bw / 2, by + bh / 2), txt, font=f_dur, fill="white", anchor="mm")

    if info["avatar"]:
        try:
            a = Image.open(io.BytesIO(http_get(info["avatar"]))).convert("RGB")
            card.paste(a.resize((av, av), Image.LANCZOS), (pad, THUMB_H + pad), mask_circle(av))
        except Exception as e:  # noqa: BLE001
            log("avatar non scaricato:", e)
            text_x = pad
    else:
        text_x = pad
    if text_x == pad:  # senza avatar il testo prende tutta la larghezza
        lines = wrap(title, f_title, CARD_W - 2 * pad, 3)

    y = THUMB_H + pad - 4
    for line in lines:
        d.text((text_x, y), line, font=f_title, fill=(15, 15, 15))
        y += title_lh
    d.text((text_x, y + 10), info["channel"], font=f_chan, fill=(96, 96, 96))

    canvas.paste(card, (CARD_X, card_y), mask_rounded((CARD_W, card_h), RADIUS))
    draw_logo(canvas, card_y + card_h + logo_gap, logo_h)

    story, solo = io.BytesIO(), io.BytesIO()
    canvas.save(story, "PNG", optimize=True)
    card_rgba = card.convert("RGBA")  # solo la card, angoli arrotondati trasparenti
    card_rgba.putalpha(mask_rounded((CARD_W, card_h), RADIUS))
    card_rgba.save(solo, "PNG", optimize=True)
    return story.getvalue(), solo.getvalue()


def draw_logo(canvas, y, h):
    """Logo YouTube bianco: rettangolo rosso col triangolo + scritta stretta."""
    f = font("InterDisplay-Bold.ttf", int(h * 1.05))
    iw, gap = int(h * 1.42), int(h * 0.14)
    tw, th = int(f.getlength("YouTube")), int(h * 1.4)
    word = Image.new("L", (tw, th), 0)
    ImageDraw.Draw(word).text((0, th / 2), "YouTube", font=f, fill=255, anchor="lm")
    word = word.resize((int(tw * 0.8), th), Image.LANCZOS)  # condensato come il logo vero
    x = (W - iw - gap - word.width) // 2
    canvas.paste(Image.new("RGB", (iw, h), (255, 0, 0)), (x, y), mask_rounded((iw, h), int(h * 0.26)))
    d = ImageDraw.Draw(canvas)
    cx, cy, t = x + iw / 2, y + h / 2, h * 0.2
    d.polygon([(cx - t * 0.8, cy - t), (cx - t * 0.8, cy + t), (cx + t * 1.05, cy)], fill="white")
    canvas.paste(Image.new("RGB", word.size, "white"), (x + iw + gap, y + (h - th) // 2), word)


def story_link(vid):
    return f"{LINK_BASE}/{vid}" if LINK_BASE else f"https://www.youtube.com/watch?v={vid}"


# ----------------------------------------------------------------- telegram
def multipart(fields, files):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"'
                      f'\r\n\r\n{v}\r\n').encode("utf-8"))
    for name, fname, data, ctype in files:
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                      f'filename="{fname}"\r\nContent-Type: {ctype}\r\n\r\n').encode("utf-8")
                     + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def tg(method, fields=None, files=None, tries=5, need_chat=True):
    url = f"{API}/bot{TOKEN}/{method}"
    fields = dict(fields or {})
    if need_chat:
        fields.setdefault("chat_id", CHAT_ID)
    for _ in range(tries):
        if files:
            body, ctype = multipart(fields, files)
        else:
            body = urllib.parse.urlencode(fields).encode()
            ctype = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                ra = json.loads(raw).get("parameters", {}).get("retry_after")
            except Exception:  # noqa: BLE001
                ra = None
            if e.code == 429 and ra:
                time.sleep(ra + 1)
                continue
            if e.code >= 500:
                time.sleep(2)
                continue
            raise RuntimeError(f"Telegram {method} {e.code}: {raw[:300]}") from None
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2)
    raise RuntimeError(f"Telegram {method}: troppi tentativi")


def send_text(text, **extra):
    tg("sendMessage", {"text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": "true", **extra})


def leggi_amazon(l):
    """(link affiliato, pagina prodotto se già scaricata). Una sola richiesta: segue il link corto e porta la pagina."""
    try:
        final, page = amz_get(l.rstrip(".,;:!?"))
    except Exception as e:  # noqa: BLE001
        log("link non aperto:", l, e)
        final, page = None, None
    link = affilia(l, final)
    return link, page if final and ASIN_RE.search(urllib.parse.urlparse(final).path) else None


def handle_update(u):
    c = (u.get("message") or u.get("my_chat_member") or {}).get("chat", {})
    if c.get("type") in ("group", "supergroup", "channel"):
        tg("leaveChat", {"chat_id": c["id"]}, tries=1, need_chat=False)
        return
    m = u.get("message") or {}
    if str(c.get("id")) != CHAT_ID:  # risponde solo a te
        return
    text = m.get("text") or m.get("caption") or ""
    vid = video_id(text)
    if text.startswith("/storia"):
        links = AMZ_RE.findall(text)
        if not links:
            send_text("Scrivi /storia e il link Amazon. Sotto puoi cambiare i campi, uno per riga:\n"
                      "<code>nome: AirPods Max\nprezzo: 419\nprima: 579\netichetta: Black Friday\nnota: Fino al 30/11</code>")
            return
        tg("sendChatAction", {"action": "upload_document"}, tries=1)
        link, page = leggi_amazon(links[0])
        info = amazon_info(link, page)
        if not info:
            raise RuntimeError("non è la pagina di un prodotto")
        tg("sendDocument", {"caption": f"<code>{html.escape(link)}</code>", "parse_mode": "HTML",
                            "reply_to_message_id": m["message_id"]},
           [("document", "storia-amazon.png", render_storia(info, dict(CAMPI_RE.findall(text))), "image/png")])
        return
    if not vid:
        links = AMZ_RE.findall(text)
        if links:  # Amazon: link affiliato da copiare, uno per riga
            for l in links:
                try:
                    link, page = leggi_amazon(l)
                except Exception as e:  # noqa: BLE001
                    log("link non affiliato:", l, e)
                    send_text(f"⚠️ Non riesco ad aprire questo link, riprova tra poco:\n{html.escape(l)}")
                    continue
                try:
                    info = amazon_info(link, page)
                    if info:
                        tg("sendDocument", {"reply_to_message_id": m["message_id"]},
                           [("document", "amazon.png", render_amazon(info), "image/png")])
                except Exception as e:  # noqa: BLE001
                    log("pagina Amazon non letta:", e)
                    send_text("Non riesco a leggere la pagina, ecco solo il link.")
                send_text(f"<code>{html.escape(link)}</code>")
        elif text.startswith("/start"):
            send_text("Mandami un link YouTube e ti preparo la storia, o un link Amazon e ti do quello affiliato.\n"
                      "Con /storia e un link Amazon ti preparo la storia dell'offerta.")
        else:
            send_text("Non vedo un link YouTube o Amazon in questo messaggio.")
        return
    tg("sendChatAction", {"action": "upload_document"}, tries=1)
    info = fetch_info(vid)
    story, solo = render(info)
    tg("sendDocument", {"caption": f"<b>{html.escape(info['title'])}</b>",
                        "parse_mode": "HTML", "reply_to_message_id": m["message_id"]},
       [("document", f"storia-{vid}.png", story, "image/png")])
    tg("sendDocument", {}, [("document", f"copertina-{vid}.png", solo, "image/png")])


def run():
    if not TOKEN or not CHAT_ID:
        sys.exit("Manca TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID: compila .env (vedi .env.example).")
    offset = 0
    log("bot attivo")
    while True:
        try:
            res = tg("getUpdates", {"offset": offset, "timeout": 50,
                                    "allowed_updates": '["message","my_chat_member"]'},
                     tries=1, need_chat=False)
        except Exception as e:  # noqa: BLE001
            log("getUpdates:", e)
            time.sleep(5)
            continue
        for u in res.get("result", []):
            offset = u["update_id"] + 1
            try:
                handle_update(u)
            except Exception as e:  # noqa: BLE001
                log("messaggio fallito:", e)
                try:
                    send_text(f"⚠️ Non sono riuscito a fare la storia: {html.escape(str(e))[:300]}")
                except Exception:  # noqa: BLE001
                    pass


def cmd_chat_id():
    for u in tg("getUpdates", need_chat=False).get("result", []):
        c = (u.get("message") or {}).get("chat")
        if c:
            print(f"TELEGRAM_CHAT_ID={c['id']}   ({c.get('username') or c.get('first_name')})")


def cmd_test(text):
    if not video_id(text) and AMZ_RE.search(text):
        link = affilia(AMZ_RE.search(text)[0])
        print(link)
        info = amazon_info(link)
        if info:
            (BASE / "amazon.png").write_bytes(render_amazon(info))
            print({k: v for k, v in info.items() if k != "image"})
        return
    vid = video_id(text)
    assert vid, "link YouTube non riconosciuto"
    info = fetch_info(vid)
    story, solo = render(info)
    (BASE / "storia.png").write_bytes(story)
    (BASE / "copertina.png").write_bytes(solo)
    print("storia.png  ·", story_link(vid), "·", info["title"], "·", info["duration"])


def selftest():
    for s in ("https://youtu.be/dQw4w9WgXcQ?si=x", "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3",
              "youtube.com/shorts/dQw4w9WgXcQ", "https://m.youtube.com/watch?feature=share&v=dQw4w9WgXcQ",
              "https://www.youtube.com/live/dQw4w9WgXcQ"):
        assert video_id(s) == "dQw4w9WgXcQ", s
    assert video_id("https://example.com/watch?v=dQw4w9WgXcQ") is None
    assert fmt_duration(839) == "13:59" and fmt_duration(3725) == "1:02:05"


if __name__ == "__main__":
    selftest()
    if "--chat-id" in sys.argv:
        cmd_chat_id()
    elif "--test" in sys.argv:
        cmd_test(sys.argv[-1])
    else:
        run()
