#!/usr/bin/env python3
"""
DOHM Farming v13 (FULL)
- Target 25000 pts
- Notif Telegram tiap +25 pts (bukan tiap tx)
- Auto claim faucet di /app/setup (Link -> wallet Linked -> Continue -> Get BTC -> Get frBTC)
- Cek faucet tiap 6-12 jam random
- Auto retry, auto run 24/7, auto update
"""
import os
import sys
import time
import re
import random
import fcntl
import hashlib
import traceback
import urllib.request
import urllib.parse
from datetime import datetime

from camoufox.sync_api import Camoufox

# ═══════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════
WALLET_PASSWORD = os.environ.get('WALLET_PASSWORD')
SEED_PHRASE     = os.environ.get('SEED_PHRASE')
if not WALLET_PASSWORD or not SEED_PHRASE:
    print("[FATAL] WALLET_PASSWORD & SEED_PHRASE wajib di-set.")
    sys.exit(1)

POINTS_TARGET = float(os.environ.get('POINTS_TARGET', '25000'))
MAX_CYCLES    = int(os.environ.get('MAX_CYCLES', '0'))
WAIT_MINUTES  = int(os.environ.get('WAIT_MINUTES', '10'))
WAIT_JITTER   = int(os.environ.get('WAIT_JITTER', '60'))
STAKE_AMOUNT  = float(os.environ.get('STAKE_AMOUNT', '0.2'))
UNSTAKE_AMOUNT= float(os.environ.get('UNSTAKE_AMOUNT', '0.1'))

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID   = os.environ.get('TELEGRAM_CHAT_ID', '')
NOTIFY_EVERY_PTS   = float(os.environ.get('NOTIFY_EVERY_PTS', '25'))

FAUCET_ENABLED   = int(os.environ.get('FAUCET_ENABLED', '1'))
FAUCET_MIN_HOURS = float(os.environ.get('FAUCET_MIN_HOURS', '6'))
FAUCET_MAX_HOURS = float(os.environ.get('FAUCET_MAX_HOURS', '12'))

AUTO_UPDATE              = int(os.environ.get('AUTO_UPDATE', '0'))
UPDATE_URL               = os.environ.get('UPDATE_URL', '')
UPDATE_CHECK_EVERY_CYCLE = int(os.environ.get('UPDATE_CHECK_EVERY_CYCLE', '5'))

URL_STAKE     = 'https://testnet.dohm.finance/app/stake'
URL_PORTFOLIO = 'https://testnet.dohm.finance/app/portfolio'
URL_SETUP     = 'https://testnet.dohm.finance/app/setup'
LOCK_FILE     = '/tmp/dohm_farm.lock'
LOG_FILE      = '/tmp/dohm_farm.log'
HEARTBEAT_FILE= '/tmp/dohm_farm.heartbeat'
SCRIPT_PATH   = os.path.abspath(__file__)

# ═══════════════════════════════════════════
# LOG + NOTIF
# ═══════════════════════════════════════════
def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass

def notify(msg):
    log(f"[NOTIF] {msg}")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            'chat_id': TELEGRAM_CHAT_ID,
            'text': msg,
            'parse_mode': 'HTML',
            'disable_web_page_preview': 'true',
        }).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
    except Exception as e:
        log(f"[NOTIF] gagal: {e}")

def heartbeat(payload: str):
    try:
        with open(HEARTBEAT_FILE, 'w') as f:
            f.write(f"{datetime.now().isoformat()} | {payload}\n")
    except Exception:
        pass

# ═══════════════════════════════════════════
# AUTO UPDATE
# ═══════════════════════════════════════════
def check_and_update():
    if not AUTO_UPDATE or not UPDATE_URL:
        return False
    try:
        log(f"[UPDATE] cek {UPDATE_URL}")
        req = urllib.request.Request(UPDATE_URL, headers={'User-Agent': 'dohm-farm'})
        with urllib.request.urlopen(req, timeout=20) as r:
            remote = r.read()
        local = open(SCRIPT_PATH, 'rb').read()
        if hashlib.sha256(remote).hexdigest() == hashlib.sha256(local).hexdigest():
            log("[UPDATE] sudah terbaru")
            return False
        log(f"[UPDATE] versi baru! {hashlib.sha256(local).hexdigest()[:8]} -> {hashlib.sha256(remote).hexdigest()[:8]}")
        with open(SCRIPT_PATH + '.bak', 'wb') as f:
            f.write(local)
        with open(SCRIPT_PATH, 'wb') as f:
            f.write(remote)
        os.chmod(SCRIPT_PATH, 0o755)
        notify("🔄 <b>Auto-update</b> — restart otomatis...")
        time.sleep(2)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        log(f"[UPDATE] error: {e}")
    return False

# ═══════════════════════════════════════════
# LOCK
# ═══════════════════════════════════════════
def acquire_lock():
    f = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        log("[LOCK] Script lain masih jalan.")
        return None

# ═══════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════
def sleep_minutes(minutes, label=""):
    total = minutes * 60 + random.randint(-WAIT_JITTER, WAIT_JITTER)
    total = max(total, 60)
    end = time.time() + total
    while time.time() < end:
        time.sleep(min(30, end - time.time()))
    log(f"  [wait] {label} done ({total // 60}m{total % 60}s)")

def get_tx_hash(page):
    try:
        txt = page.inner_text('body') or ''
        for pat in (r'(0x[a-fA-F0-9]{64})', r'([a-f0-9]{8,}\.[a-f0-9]{4,})', r'\b([a-f0-9]{64})\b'):
            m = re.findall(pat, txt)
            if m:
                return m[-1]
    except Exception:
        pass
    return None

def read_points_no_nav(page):
    try:
        txt = page.inner_text('body') or ''
        for pat in (r'(\d+\.?\d*)\s*pts', r'[Pp]oints?\s*[:=]?\s*(\d+\.?\d*)'):
            m = re.search(pat, txt, re.IGNORECASE)
            if m:
                return float(m.group(1))
    except Exception:
        pass
    return None

def get_points(page):
    try:
        page.goto(URL_STAKE, wait_until='load', timeout=30000)
        for _ in range(15):
            time.sleep(3)
            pts = read_points_no_nav(page)
            if pts and pts > 0:
                return pts
        return 0
    except Exception as e:
        log(f"  [get_points error] {e}")
        return 0

# ═══════════════════════════════════════════
# WALLET
# ═══════════════════════════════════════════
def check_wallet_status(page):
    if page.locator('button:has-text("Connect wallet"):visible').count() > 0:
        return 'needs_connect'
    try:
        body = page.inner_text('body')
    except Exception:
        return 'needs_connect'
    if re.search(r'\bbcrt1q[a-z0-9]{20,}\b', body) or re.search(r'\b0x[a-fA-F0-9]{40}\b', body):
        return 'connected'
    for label in ['Stake DOHM', 'Unstake DOHM']:
        if page.locator(f'button:has-text("{label}"):not([disabled]):visible').count() > 0:
            return 'connected'
    if page.locator('input[type="password"]').count() > 0:
        return 'needs_unlock'
    return 'needs_connect'

def unlock_wallet(page):
    pw = page.locator('input[type="password"]')
    if pw.count() > 0:
        log("  [wallet] unlocking...")
        pw.fill(WALLET_PASSWORD)
        time.sleep(1)
        u = page.locator('button:has-text("Unlock"):visible')
        if u.count() > 0:
            u.first.click()
            time.sleep(4)

def restore_wallet(page):
    log("  [wallet] clicking Connect wallet...")
    try:
        page.locator('button:has-text("Connect wallet"):visible').first.click()
    except Exception:
        return False
    time.sleep(2)
    r = page.locator('button:has-text("Restore from recovery phrase"):visible')
    if r.count() == 0:
        return False
    r.first.click()
    time.sleep(2)
    ta = page.locator('textarea')
    if ta.count() > 0:
        ta.fill(SEED_PHRASE)
        time.sleep(0.5)
    pw = page.locator('input[type="password"]')
    if pw.count() > 0:
        pw.fill(WALLET_PASSWORD)
        time.sleep(0.5)
    rb = page.locator('button:has-text("Restore"):visible')
    if rb.count() > 0:
        rb.first.click()
        time.sleep(10)
    page.goto(URL_STAKE, wait_until='load', timeout=30000)
    time.sleep(8)
    return check_wallet_status(page) == 'connected'

def ensure_wallet(page):
    s = check_wallet_status(page)
    log(f"  [wallet] status: {s}")
    if s == 'connected':
        return True
    if s == 'needs_unlock':
        unlock_wallet(page)
        return check_wallet_status(page) == 'connected'
    if restore_wallet(page):
        return True
    unlock_wallet(page)
    return check_wallet_status(page) == 'connected'

# ═══════════════════════════════════════════
# FORM + RETRY
# ═══════════════════════════════════════════
def fill_amount(page, amount):
    inp = page.locator('input:visible:not([disabled])').first
    if inp.count() == 0:
        return 'none'
    inp.click()
    inp.fill('')
    time.sleep(0.3)
    inp.fill(str(amount))
    time.sleep(2)
    return inp.input_value()

def verify_tab(page):
    a = page.locator('button[aria-selected="true"]').first
    return a.inner_text().strip() if a.count() > 0 else 'none'

def click_confirm_sign(page, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        b = page.locator('button:has-text("Confirm & sign"):not([disabled]):visible')
        if b.count() > 0:
            try:
                b.first.scroll_into_view_if_needed()
                b.first.click()
                time.sleep(4)
                return 'confirmed'
            except Exception:
                pass
        if get_tx_hash(page):
            return 'confirmed'
        time.sleep(1)
    return 'no-confirm'

def retry_action(fn, tries=3, base_delay=5, label=""):
    for i in range(tries):
        try:
            r = fn()
            if r not in ('failed-3x', 'no-confirm', None):
                return r
            log(f"  [retry:{label}] {i + 1}/{tries} -> {r}")
        except Exception as e:
            log(f"  [retry:{label}] {i + 1}/{tries} err: {e}")
        if i < tries - 1:
            time.sleep(base_delay * (2 ** i))
    return 'failed-3x'

# ═══════════════════════════════════════════
# ACTIONS
# ═══════════════════════════════════════════
def stake(page, amount=0.2):
    page.goto(URL_STAKE, wait_until='load', timeout=30000)
    for _ in range(6):
        time.sleep(3)
        if page.locator('button:has-text("Stake"):visible').count() > 0:
            break
    for attempt in range(3):
        active = verify_tab(page)
        if active != 'Stake':
            t = page.locator('button:has-text("Stake"):visible')
            if t.count() > 0:
                t.first.click()
            time.sleep(3)
            active = verify_tab(page)
        if active != 'Stake':
            time.sleep(3)
            continue
        val = fill_amount(page, amount)
        log(f"  input: '{val}'")
        s = page.locator('button:has-text("Stake DOHM"):not([disabled]):visible')
        if s.count() > 0:
            s.first.scroll_into_view_if_needed()
            s.first.click()
            time.sleep(2)
            conf = click_confirm_sign(page)
            log(f"  stake tx: {get_tx_hash(page)} | confirm: {conf}")
            return 'done' if conf == 'confirmed' else conf
        time.sleep(2)
    return 'failed-3x'

def get_sohm_balance(page):
    try:
        txt = page.inner_text('body') or ''
        for pat in (r'(\d+\.?\d*)\s*sDOHM', r'[Bb]alance\s+(\d+\.?\d*)\s*sDOHM'):
            m = re.search(pat, txt, re.IGNORECASE)
            if m:
                return float(m.group(1))
    except Exception:
        pass
    return 0

def unstake(page, amount=0.1):
    page.goto(URL_STAKE, wait_until='load', timeout=30000)
    for _ in range(6):
        time.sleep(3)
        if page.locator('button:has-text("Unstake"):visible').count() > 0:
            break
    active = verify_tab(page)
    if active != 'Unstake':
        t = page.locator('button:has-text("Unstake"):visible')
        if t.count() > 0:
            t.first.click()
        time.sleep(3)
    bal = get_sohm_balance(page)
    if bal <= 0:
        log(f"  no sDOHM ({bal})")
        return 'skip'
    amt = amount if amount < bal else round(max(bal - 0.001, 0), 4)
    if amt <= 0:
        return 'skip'
    log(f"  sDOHM: {bal:.4f}, unstaking: {amt}")
    for attempt in range(3):
        active = verify_tab(page)
        if active != 'Unstake':
            t = page.locator('button:has-text("Unstake"):visible')
            if t.count() > 0:
                t.first.click()
            time.sleep(3)
            active = verify_tab(page)
        if active != 'Unstake':
            time.sleep(3)
            continue
        val = fill_amount(page, amt)
        log(f"  input: '{val}'")
        s = page.locator('button:has-text("Unstake DOHM"):visible')
        if s.count() > 0 and s.first.get_attribute('disabled', timeout=1000) is None:
            s.first.scroll_into_view_if_needed()
            s.first.click()
            time.sleep(2)
            conf = click_confirm_sign(page)
            log(f"  unstake tx: {get_tx_hash(page)} | confirm: {conf}")
            return 'done' if conf == 'confirmed' else conf
        time.sleep(3)
    return 'failed-3x'

def claim_matured_bonds(page):
    page.goto(URL_PORTFOLIO, wait_until='load', timeout=30000)
    for _ in range(20):
        time.sleep(1)
        if page.locator('button:has-text("Claim"):visible').count() > 0:
            break
    ensure_wallet(page)
    time.sleep(3)
    total = 0
    for rnd in range(10):
        n = page.locator('button:has-text("Claim"):not([disabled]):visible').count()
        if n == 0:
            break
        log(f"  [claim] round {rnd + 1}: {n} tombol")
        for _ in range(n):
            b = page.locator('button:has-text("Claim"):not([disabled]):visible').first
            if b.count() == 0:
                break
            try:
                b.scroll_into_view_if_needed()
                b.click()
                total += 1
                time.sleep(8)
            except Exception as e:
                log(f"  [claim] err: {e}")
                break
        try:
            page.reload(wait_until='load', timeout=30000)
            time.sleep(8)
        except Exception:
            break
    log(f"  [claim] total: {total}")
    return 'done' if total > 0 else 'skip'

# ═══════════════════════════════════════════
# FAUCET (Link -> wallet Linked -> Continue -> Get BTC -> Get frBTC)
# ═══════════════════════════════════════════
def _safe_click(page, locator, label="", timeout=5000):
    try:
        if locator.count() == 0:
            return False
        el = locator.first
        el.scroll_into_view_if_needed(timeout=timeout)
        disabled = el.get_attribute('disabled', timeout=timeout)
        if disabled is not None:
            log(f"  [faucet] '{label}' disabled")
            return False
        el.click(timeout=timeout)
        return True
    except Exception as e:
        log(f"  [faucet] klik '{label}' err: {e}")
        return False

def _find_visible(page, selectors):
    for sel in selectors:
        try:
            loc = page.locator(sel)
            n = loc.count()
            for i in range(n):
                try:
                    if loc.nth(i).is_visible():
                        return loc.nth(i)
                except Exception:
                    continue
        except Exception:
            continue
    return None

def try_claim_faucet(page):
    try:
        log(f"[FAUCET] buka {URL_SETUP}")
        page.goto(URL_SETUP, wait_until='load', timeout=30000)
        for _ in range(15):
            time.sleep(2)
            try:
                if page.locator('button:visible').count() > 0:
                    break
            except Exception:
                pass
        ensure_wallet(page)
        time.sleep(2)

        # STEP 1: menu Link
        link_menu = _find_visible(page, [
            'a:has-text("Link")',
            'button:has-text("Link")',
            '[role="tab"]:has-text("Link")',
            'text=/^Link$/i',
        ])
        if not link_menu:
            log("[FAUCET] menu 'Link' ga ketemu")
            return 'not-available'
        log("[FAUCET] klik menu Link")
        try:
            link_menu.scroll_into_view_if_needed()
            link_menu.click()
        except Exception as e:
            log(f"[FAUCET] klik Link err: {e}")
            return 'error'
        time.sleep(3)

        # STEP 2: wallet Linked
        wl = _find_visible(page, [
            'button:has-text("wallet Linked")',
            'button:has-text("Wallet Linked")',
            'button:has-text("Linked")',
            '[role="button"]:has-text("Linked")',
            'text=/wallet\\s+Linked/i',
            'text=/Linked/i',
        ])
        if wl:
            log("[FAUCET] klik 'wallet Linked'")
            try:
                wl.scroll_into_view_if_needed()
                wl.click()
                time.sleep(3)
            except Exception as e:
                log(f"[FAUCET] klik wallet Linked err: {e}")
        else:
            log("[FAUCET] tombol 'wallet Linked' ga ketemu (lanjut)")

        # STEP 3: Continue
        cont = _find_visible(page, [
            'button:has-text("Continue")',
            'button:has-text("CONTINUE")',
            '[role="button"]:has-text("Continue")',
        ])
        if cont:
            log("[FAUCET] klik Continue")
            try:
                cont.scroll_into_view_if_needed()
                cont.click()
                time.sleep(4)
            except Exception as e:
                log(f"[FAUCET] klik Continue err: {e}")
        else:
            log("[FAUCET] tombol Continue ga ketemu (lanjut)")

        time.sleep(3)
        claimed_any = False

        # STEP 4: Get BTC
        btc_btn = _find_visible(page, [
            'button:has-text("Get BTC")',
            'button:has-text("GET BTC")',
            'button:has-text("Get btc")',
            '[role="button"]:has-text("Get BTC")',
            'text=/Get\\s+BTC/i',
        ])
        if btc_btn:
            log("[FAUCET] klik Get BTC")
            if _safe_click(page, btc_btn, "Get BTC"):
                time.sleep(3)
                conf = click_confirm_sign(page, timeout=20)
                tx = get_tx_hash(page)
                log(f"[FAUCET] BTC confirm={conf} tx={tx}")
                if conf == 'confirmed' or tx:
                    claimed_any = True
                time.sleep(5)
        else:
            log("[FAUCET] tombol 'Get BTC' ga ketemu")

        # STEP 5: Get frBTC
        frbtc_btn = _find_visible(page, [
            'button:has-text("Get frBTC")',
            'button:has-text("GET frBTC")',
            'button:has-text("Get frbtc")',
            'button:has-text("frBTC")',
            '[role="button"]:has-text("frBTC")',
            'text=/Get\\s+frBTC/i',
        ])
        if frbtc_btn:
            log("[FAUCET] klik Get frBTC")
            if _safe_click(page, frbtc_btn, "Get frBTC"):
                time.sleep(3)
                conf = click_confirm_sign(page, timeout=20)
                tx = get_tx_hash(page)
                log(f"[FAUCET] frBTC confirm={conf} tx={tx}")
                if conf == 'confirmed' or tx:
                    claimed_any = True
                time.sleep(5)
        else:
            log("[FAUCET] tombol 'Get frBTC' ga ketemu")

        if claimed_any:
            log("[FAUCET] ✅ minimal 1 token ke-claim")
            return 'claimed'

        try:
            body = page.inner_text('body') or ''
            if re.search(r'(cooldown|already|wait|next.*in|come back)', body, re.IGNORECASE):
                log("[FAUCET] cooldown")
                return 'cooldown'
        except Exception:
            pass
        log("[FAUCET] ga ada yang ke-claim")
        return 'not-available'

    except Exception as e:
        log(f"[FAUCET] exception: {e}")
        log(traceback.format_exc())
        return 'error'

# ═══════════════════════════════════════════
# CYCLE RUNNER
# ═══════════════════════════════════════════
def run_farming_session():
    log(f"[INIT] DOHM Farm v13 | {datetime.now()}")
    log(f"[INIT] Target: {POINTS_TARGET} pts | Notif tiap +{NOTIFY_EVERY_PTS} pts")
    log(f"[INIT] Flow: Stake {STAKE_AMOUNT} -> Wait -> Unstake {UNSTAKE_AMOUNT} -> Wait -> Claim -> Wait -> Repeat")
    if FAUCET_ENABLED:
        log(f"[INIT] Faucet: ON (cek tiap {FAUCET_MIN_HOURS}-{FAUCET_MAX_HOURS} jam)")
    notify(f"🚀 <b>DOHM Farm v13 start</b>\nTarget: {POINTS_TARGET} pts\nMode: {'infinite 24/7' if MAX_CYCLES == 0 else f'{MAX_CYCLES} cycles'}")

    with Camoufox(headless=True) as browser:
        page = browser.new_page()
        page.goto(URL_STAKE, wait_until='load', timeout=30000)
        time.sleep(5)
        ensure_wallet(page)

        initial_points = get_points(page)
        log(f"[INIT] Points: {initial_points:.2f} | Target: {POINTS_TARGET}")
        notify(f"📊 <b>Starting points:</b> {initial_points:.2f}")

        cycle_start = time.time()
        cycle_num = 0
        last_notif_pts = initial_points
        next_faucet_time = time.time()  # cek faucet segera di cycle pertama

        while True:
            cycle_num += 1
            if MAX_CYCLES > 0 and cycle_num > MAX_CYCLES:
                log(f"[END] Max cycles ({MAX_CYCLES}) reached.")
                return True

            log(f"\n{'=' * 60}")
            log(f"CYCLE {cycle_num}{'/' + str(MAX_CYCLES) if MAX_CYCLES else ' (infinite)'} | "
                f"{datetime.now().strftime('%H:%M:%S')}")
            log(f"{'=' * 60}")

            pts = get_points(page)
            heartbeat(f"cycle={cycle_num} pts={pts:.2f}")

            # ── CEK TARGET ──
            if pts >= POINTS_TARGET:
                msg = (
                    f"🎯 <b>TARGET TERCAPAI!</b>\n"
                    f"Points: <b>{pts:.2f}</b> / {POINTS_TARGET}\n"
                    f"Cycles: {cycle_num}\n"
                    f"Elapsed: {(time.time() - cycle_start) / 3600:.2f}h"
                )
                log(f"[GOAL] {msg}")
                notify(msg)
                return True

            # ── NOTIF TIAP N PTS ──
            if pts - last_notif_pts >= NOTIFY_EVERY_PTS:
                gained = pts - initial_points
                notify(f"📈 <b>{pts:.2f} pts</b> (+{gained:.2f} total)\nCycle: {cycle_num}")
                last_notif_pts = pts

            log(f"  pts: {pts:.2f}")

            # ── AUTO UPDATE CHECK ──
            if AUTO_UPDATE and cycle_num % UPDATE_CHECK_EVERY_CYCLE == 0:
                check_and_update()

            # ── FAUCET CHECK ──
            if FAUCET_ENABLED and time.time() >= next_faucet_time:
                log("  [faucet] checking...")
                faucet_result = retry_action(lambda: try_claim_faucet(page), tries=2, label="faucet")
                log(f"  [faucet] result: {faucet_result}")
                if faucet_result in ('claimed', 'cooldown'):
                    # Cooldown atau sukses → jadwalkan berikutnya
                    delay_hours = random.uniform(FAUCET_MIN_HOURS, FAUCET_MAX_HOURS)
                    next_faucet_time = time.time() + delay_hours * 3600
                    log(f"  [faucet] next check in {delay_hours:.1f}h")
                elif faucet_result == 'not-available':
                    # Faucet belum available, cek lagi dalam 1 jam
                    next_faucet_time = time.time() + 3600
                    log("  [faucet] not available, retry in 1h")
                else:
                    # Error → cek lagi dalam 30 menit
                    next_faucet_time = time.time() + 1800
                    log("  [faucet] error, retry in 30m")
                # Navigate back to stake page after faucet
                page.goto(URL_STAKE, wait_until='load', timeout=30000)
                time.sleep(3)

            # ── STAKE ──
            log(f"  [1/3] Stake {STAKE_AMOUNT} DOHM...")
            r1 = retry_action(lambda: stake(page, STAKE_AMOUNT), tries=3, label="stake")
            log(f"  -> {r1}")
            if r1 != 'done':
                log(f"  [!] stake gagal ({r1}), skip cycle")
                notify(f"⚠️ Stake gagal di cycle {cycle_num}: {r1}")
                time.sleep(60)
                continue

            sleep_minutes(WAIT_MINUTES, "setelah stake")

            # ── UNSTAKE ──
            log(f"  [2/3] Unstake {UNSTAKE_AMOUNT} sDOHM...")
            r2 = retry_action(lambda: unstake(page, UNSTAKE_AMOUNT), tries=3, label="unstake")
            log(f"  -> {r2}")
            if r2 == 'done':
                sleep_minutes(WAIT_MINUTES, "setelah unstake")
            elif r2 == 'skip':
                log("  no sDOHM, lanjut ke claim")

            # ── CLAIM ──
            log(f"  [3/3] Claim matured bonds...")
            r3 = retry_action(lambda: claim_matured_bonds(page), tries=2, label="claim")
            log(f"  -> {r3}")
            sleep_minutes(WAIT_MINUTES, "setelah claim")

            # ── CYCLE DONE ──
            elapsed = (time.time() - cycle_start) / 3600
            pts_now = get_points(page)
            gained = pts_now - initial_points
            log(f"  cycle {cycle_num} done. pts={pts_now:.2f} | elapsed={elapsed:.2f}h | gained={gained:.2f}")

            # Notif tiap +N pts
            if pts_now - last_notif_pts >= NOTIFY_EVERY_PTS:
                notify(f"📈 <b>{pts_now:.2f} pts</b> (+{gained:.2f} total)\nCycle: {cycle_num}")
                last_notif_pts = pts_now

# ═══════════════════════════════════════════
# SUPERVISOR — ⚠️ TERPOTONG
# ═══════════════════════════════════════════
if __name__ == '__main__':
    lock = acquire_lock()
    if not lock:
        sys.exit(1)

    restart_count = 0
    while True:
        try:
            done = run_farming_session()
            if done:
                log("[SUPERVISOR] Selesai. Exit.")
                notify("✅ <b>DOHM Farm selesai</b>")
                break
            else:
                log("[SUPERVISOR] Session ended, restart in 30s...")
                time.sleep(30)
        except KeyboardInterrupt:
            log("[SUPERVISOR] Ctrl+C, exit.")
            notify("🛑 DOHM Farm dihentikan manual.")
            break
        except Exception as e:
            restart_count += 1
            log(f"[SUPERVISOR] Crash #{restart_count}: {e}")
            log(traceback.format_exc())
            notify(f"⚠️ Crash #{restart_count}: {e}")
            delay = min(300, 30 * restart_count)
            log(f"[SUPERVISOR] Restart in {delay}s...")
            time.sleep(delay)
