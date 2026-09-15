#!/usr/bin/env python3
"""
DOHM Farming v19 + Fix Selector
- Sequential (no threads — Playwright sync API thread-unsafe)
- Selector fleksibel (regex + scan + aria fallback)
- Auto-recover wallet kalau tiba-tiba needs_connect
- Debug dump tombol kalau gagal
- Auto-restart supervisor
"""
import os
import sys
import time
import re
import random
import fcntl
import hashlib
import traceback
import threading
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
STAKE_AMOUNT  = float(os.environ.get('STAKE_AMOUNT', '0.2'))
UNSTAKE_AMOUNT= float(os.environ.get('UNSTAKE_AMOUNT', '0.1'))

JEDA_MIN = int(os.environ.get('JEDA_MIN', '5'))
JEDA_MAX = int(os.environ.get('JEDA_MAX', '15'))

TX_WAIT_MAX      = int(os.environ.get('TX_WAIT_MAX', '900'))
TX_POLL_INTERVAL = int(os.environ.get('TX_POLL_INTERVAL', '5'))
GRACE_MIN = int(os.environ.get('GRACE_MIN', '10'))
GRACE_MAX = int(os.environ.get('GRACE_MAX', '20'))
CLAIM_MAX_WAIT = int(os.environ.get('CLAIM_MAX_WAIT', '600'))

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID   = os.environ.get('TELEGRAM_CHAT_ID', '')
NOTIFY_EVERY_PTS   = float(os.environ.get('NOTIFY_EVERY_PTS', '25'))

FAUCET_ENABLED   = int(os.environ.get('FAUCET_ENABLED', '1'))
FAUCET_MIN_HOURS = float(os.environ.get('FAUCET_MIN_HOURS', '6'))
FAUCET_MAX_HOURS = float(os.environ.get('FAUCET_MAX_HOURS', '12'))

AUTO_UPDATE = int(os.environ.get('AUTO_UPDATE', '0'))
UPDATE_URL  = os.environ.get('UPDATE_URL', '')

URL_STAKE     = 'https://testnet.dohm.finance/app/stake'
URL_PORTFOLIO = 'https://testnet.dohm.finance/app/portfolio'
URL_SETUP     = 'https://testnet.dohm.finance/app/setup'
LOCK_FILE     = '/tmp/dohm_farm.lock'
LOG_FILE      = '/tmp/dohm_farm.log'
HEARTBEAT_FILE= '/tmp/dohm_farm.heartbeat'
SCRIPT_PATH   = os.path.abspath(__file__)

# ═══════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════
state = {
    'stop': False,
    'initial_points': 0.0,
    'last_notify_pts': 0.0,
    'target_reached': False,
    'wallet_ok': False,
    'current_cycle': 0,
    'stake_fail_streak': 0,
}

start_time = time.time()
_log_lock = threading.Lock()

# ═══════════════════════════════════════════
# LOG + NOTIF
# ═══════════════════════════════════════════
def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    with _log_lock:
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
# DOM HELPERS (fleksibel button detection)
# ═══════════════════════════════════════════
def wait_dom_stable(page, timeout=15):
    """Tunggu DOM stabil (ga berubah 2x berturut-turut)."""
    end = time.time() + timeout
    prev_len = -1
    stable_count = 0
    while time.time() < end:
        try:
            cur_len = page.evaluate("() => document.body.innerHTML.length")
        except Exception:
            time.sleep(1)
            continue
        if cur_len == prev_len:
            stable_count += 1
            if stable_count >= 2:
                return True
        else:
            stable_count = 0
        prev_len = cur_len
        time.sleep(1)
    return False

def find_action_button(page, action, timeout=30):
    """
    Cari tombol aksi (Stake/Unstake/Claim) dengan selector fleksibel.
    action: 'stake' | 'unstake' | 'claim'
    Return: locator atau None
    """
    patterns = {
        'stake':   [r'^Stake$', r'^Stake\s+DOHM$', r'^STAKE', r'Stake'],
        'unstake': [r'^Unstake$', r'^Unstake\s+DOHM$', r'^UNSTAKE', r'Unstake'],
        'claim':   [r'^Claim$', r'^CLAIM', r'Claim'],
    }
    end = time.time() + timeout
    while time.time() < end:
        # cara 1: has-text exact
        for pat in patterns.get(action, []):
            clean_pat = pat.strip("^$")
            try:
                loc = page.locator(f'button:has-text("{clean_pat}")')
                for i in range(loc.count()):
                    el = loc.nth(i)
                    try:
                        if not el.is_visible():
                            continue
                        txt = el.inner_text().strip()
                        if re.match(pat, txt, re.IGNORECASE):
                            return el
                    except Exception:
                        continue
            except Exception:
                pass

        # cara 2: scan semua button, filter by text
        try:
            all_btns = page.locator('button:visible')
            n = all_btns.count()
            for i in range(min(n, 50)):
                try:
                    el = all_btns.nth(i)
                    txt = el.inner_text().strip()
                    for pat in patterns.get(action, []):
                        if re.match(pat, txt, re.IGNORECASE):
                            return el
                except Exception:
                    continue
        except Exception:
            pass

        # cara 3: aria-label / data-testid fallback
        try:
            for sel in [
                f'button[aria-label*="{action}" i]',
                f'button[data-testid*="{action}" i]',
                f'[role="button"][aria-label*="{action}" i]',
            ]:
                loc = page.locator(sel)
                for i in range(loc.count()):
                    el = loc.nth(i)
                    if el.is_visible():
                        return el
        except Exception:
            pass

        time.sleep(1)
    return None

def click_button_safe(page, btn, label="", force_fallback=True):
    """
    Klik tombol dengan aman:
    1. scroll into view
    2. cek enabled
    3. klik normal
    4. fallback: force click kalau enabled tapi ga bisa diklik
    5. fallback terakhir: JS click
    """
    try:
        btn.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass

    # cek disabled
    try:
        dis = btn.get_attribute('disabled', timeout=2000)
        if dis is not None:
            log(f"  [click] '{label}' disabled")
            return 'disabled'
    except Exception:
        pass

    # coba klik normal
    try:
        btn.click(timeout=5000)
        return 'ok'
    except Exception as e:
        log(f"  [click] '{label}' normal click err: {e}")

    # fallback: force click
    if force_fallback:
        try:
            btn.click(force=True, timeout=5000)
            log(f"  [click] '{label}' force click OK")
            return 'ok'
        except Exception as e:
            log(f"  [click] '{label}' force click err: {e}")

    # fallback terakhir: JS click
    try:
        btn.evaluate("el => el.click()")
        log(f"  [click] '{label}' JS click OK")
        return 'ok'
    except Exception as e:
        log(f"  [click] '{label}' JS click err: {e}")

    return 'fail'

def debug_dump_buttons(page, label=""):
    """Debug: dump semua visible buttons ke log."""
    try:
        btns = page.locator('button:visible')
        n = btns.count()
        log(f"  [debug] {label} — {n} visible buttons:")
        for i in range(min(n, 20)):
            try:
                txt = btns.nth(i).inner_text().strip()[:60]
                dis = btns.nth(i).get_attribute('disabled')
                log(f"    [{i}] '{txt}' disabled={dis}")
            except Exception:
                pass
    except Exception:
        pass

# ═══════════════════════════════════════════
# WALLET
# ═══════════════════════════════════════════
def check_wallet_status(page):
    """Cek status wallet: connected, needs_connect, needs_fund, needs_unlock."""
    try:
        try:
            body = page.inner_text('body') or ''
        except Exception:
            return 'needs_connect'

        # CHECK BCRT ADDRESS FIRST — strongest signal wallet is connected
        if re.search(r'bcrt[\u2026\.][a-z0-9]{2,}', body) or re.search(r'\bbcrt1q[a-z0-9]{20,}\b', body):
            # Wallet address found — check if Stake is enabled or disabled
            for label in ['Stake DOHM', 'Unstake DOHM']:
                btns = page.locator(f'button:has-text("{label}"):visible')
                for i in range(btns.count()):
                    el = btns.nth(i)
                    try:
                        dis = el.get_attribute('disabled', timeout=1000)
                        if dis is None:  # enabled!
                            return 'connected'
                    except Exception:
                        pass
            # Address visible but Stake disabled = needs fund
            stake_disabled = page.locator('button[aria-label*="Stake"][disabled]:visible')
            if stake_disabled.count() > 0:
                return 'needs_fund'
            # Address visible, Stake not found yet
            return 'connected'

        # No bcrt address — check other signals
        if page.locator('button:has-text("Connect wallet"):visible').count() > 0:
            return 'needs_connect'
        if page.locator('input[type="password"]').count() > 0:
            return 'needs_unlock'
        if page.locator('text=/Create.*testnet.*wallet/i').count() > 0:
            return 'needs_create'

    except Exception:
        pass
    return 'needs_connect'

def restore_wallet(page):
    """Restore wallet dari seed phrase."""
    if not SEED_PHRASE:
        log("  [wallet] SEED_PHRASE kosong")
        return False

    log("  [wallet] RESTORE dari seed...")
    try:
        # Reload page dulu biar clean state
        page.goto(URL_STAKE, wait_until='load', timeout=30000)
        time.sleep(5)

        connect = page.locator('button:has-text("Connect wallet"):visible')
        if connect.count() > 0:
            connect.first.click()
            time.sleep(5)

        # Tunggu modal muncul (max 25s)
        restore_btn = None
        for _ in range(25):
            time.sleep(1)
            restore_btn = page.locator('button:has-text("Restore from recovery phrase"):visible')
            if restore_btn.count() > 0:
                log("  [wallet] modal ketemu")
                break

        if not restore_btn or restore_btn.count() == 0:
            # Retry: klik Connect wallet lagi
            connect2 = page.locator('button:has-text("Connect wallet"):visible')
            if connect2.count() > 0:
                connect2.first.click()
                time.sleep(5)
            restore_btn = page.locator('button:has-text("Restore from recovery phrase"):visible')

        if not restore_btn or restore_btn.count() == 0:
            log("  [wallet] 'Restore from recovery phrase' ga ketemu")
            return False

        restore_btn.first.click()
        time.sleep(3)

        # Fill seed phrase
        ta = page.locator('textarea')
        if ta.count() > 0:
            ta.fill(SEED_PHRASE)
            time.sleep(1)
            log("  [wallet] seed filled")

        # Fill password
        pw = page.locator('input[type="password"]')
        if pw.count() > 0:
            pw.fill(WALLET_PASSWORD)
            time.sleep(1)
            log("  [wallet] password filled")

        # Click Restore
        rb = page.locator('button:has-text("Restore"):visible')
        if rb.count() > 0:
            rb.first.click()
            log("  [wallet] klik Restore...")
            time.sleep(35)  # debug proved 30s works, add buffer

        # Check wallet status on CURRENT page
        status = check_wallet_status(page)
        log(f"  [wallet] result (current page): {status}")

        if status != 'connected':
            # Navigate to stake page and check again
            page.goto(URL_STAKE, wait_until='load', timeout=30000)
            time.sleep(10)
            status = check_wallet_status(page)
            log(f"  [wallet] result (stake page): {status}")

        return status in ('connected', 'needs_fund')
    except Exception as e:
        log(f"  [wallet] restore err: {e}")
        return False

def check_and_recover_wallet(page):
    """Kalau wallet tiba-tiba needs_connect, coba restore ulang."""
    s = check_wallet_status(page)
    if s == 'connected':
        return True
    log(f"  [wallet] tiba-tiba {s}, recovery...")
    return restore_wallet(page)

# ═══════════════════════════════════════════
# STAKE / UNSTAKE / CLAIM
# ═══════════════════════════════════════════
def fill_amount(page, amount):
    """Fill amount input (React-compatible)."""
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
    """Cek tab aktif (Stake/Unstake)."""
    a = page.locator('button[aria-selected="true"]').first
    return a.inner_text().strip() if a.count() > 0 else 'none'

def click_confirm_sign(page, timeout=30):
    """Cari & klik 'Confirm & sign' — fallback ke selector lain."""
    end = time.time() + timeout
    wait_dom_stable(page, timeout=10)
    while time.time() < end:
        # Cara 1: exact text
        b = page.locator('button:has-text("Confirm & sign"):visible')
        if b.count() > 0:
            try:
                el = b.first
                dis = el.get_attribute('disabled', timeout=1000)
                if dis is None:
                    el.scroll_into_view_if_needed()
                    el.click()
                    time.sleep(4)
                    return 'confirmed'
            except Exception:
                pass

        # Cara 2: text contains "Confirm"
        b2 = page.locator('button:has-text("Confirm"):visible')
        for i in range(b2.count()):
            try:
                el = b2.nth(i)
                txt = el.inner_text().strip()
                dis = el.get_attribute('disabled', timeout=1000)
                if dis is None and 'confirm' in txt.lower():
                    el.scroll_into_view_if_needed()
                    el.click()
                    time.sleep(4)
                    return 'confirmed'
            except Exception:
                pass

        # Cara 3: force click (even if disabled, just try)
        b3 = page.locator('button:has-text("Confirm & sign"):visible')
        if b3.count() > 0:
            try:
                b3.first.click(force=True, timeout=5000)
                time.sleep(4)
                return 'confirmed'
            except Exception:
                pass

        if get_tx_hash(page):
            return 'confirmed'
        time.sleep(1)
    return 'no-confirm'

def get_tx_hash(page):
    """Cek apakah ada tx hash di halaman."""
    try:
        body = page.inner_text('body') or ''
        if re.search(r'0x[a-fA-F0-9]{64}', body):
            return True
    except Exception:
        pass
    return False

def do_stake(page, amount=0.2):
    """Stake DOHM — pakai flex selector."""
    page.goto(URL_STAKE, wait_until='load', timeout=30000)
    wait_dom_stable(page, timeout=15)

    for attempt in range(3):
        active = verify_tab(page)
        if active != 'Stake':
            t = find_action_button(page, 'stake', timeout=10)
            if t:
                click_button_safe(page, t, "Stake tab")
            time.sleep(3)
            active = verify_tab(page)
        if active != 'Stake':
            time.sleep(3)
            continue

        val = fill_amount(page, amount)
        log(f"  input: '{val}'")

        s = find_action_button(page, 'stake', timeout=10)
        if s:
            txt = s.inner_text().strip()
            if 'DOHM' in txt.upper() or 'Stake' in txt:
                r = click_button_safe(page, s, "Stake DOHM")
                if r == 'disabled':
                    log("  [stake] button disabled, skip")
                    return 'disabled'
                time.sleep(2)
                conf = click_confirm_sign(page)
                log(f"  stake confirm: {conf}")
                return 'done' if conf == 'confirmed' else conf
        time.sleep(2)

    debug_dump_buttons(page, "stake failed")
    return 'failed-3x'

def do_unstake(page, amount=0.1):
    """Unstake sDOHM — pakai flex selector."""
    page.goto(URL_STAKE, wait_until='load', timeout=30000)
    wait_dom_stable(page, timeout=15)

    t = find_action_button(page, 'unstake', timeout=10)
    if t:
        click_button_safe(page, t, "Unstake tab")
    time.sleep(3)

    active = verify_tab(page)
    if active != 'Unstake':
        log(f"  tab ga pindah ke Unstake (masih {active})")
        return 'tab-fail'

    bal = get_sohm_balance(page)
    if bal <= 0:
        log(f"  no sDOHM ({bal})")
        return 'skip'
    amt = amount if amount < bal else round(max(bal - 0.001, 0), 4)
    if amt <= 0:
        return 'skip'
    log(f"  sDOHM: {bal:.4f}, unstaking: {amt}")

    val = fill_amount(page, amt)
    log(f"  input: '{val}'")

    s = find_action_button(page, 'unstake', timeout=10)
    if s:
        r = click_button_safe(page, s, "Unstake DOHM")
        if r == 'disabled':
            return 'disabled'
        time.sleep(2)
        conf = click_confirm_sign(page)
        log(f"  unstake confirm: {conf}")
        return 'done' if conf == 'confirmed' else conf

    debug_dump_buttons(page, "unstake failed")
    return 'failed'

def get_sohm_balance(page):
    """Baca sDOHM balance."""
    try:
        body = page.inner_text('body') or ''
        # Look for sDOHM balance patterns
        m = re.search(r'([\d,.]+)\s*sDOHM', body)
        if m:
            return float(m.group(1).replace(',', ''))
        m = re.search(r'Available[:\s]*([\d,.]+)', body)
        if m:
            return float(m.group(1).replace(',', ''))
    except Exception:
        pass
    return 0

def do_claim(page):
    """Claim semua matured bonds — FIRE & FORGET."""
    page.goto(URL_PORTFOLIO, wait_until='load', timeout=30000)
    for _ in range(20):
        time.sleep(1)
        if page.locator('button:has-text("Claim"):visible').count() > 0:
            break
    time.sleep(2)

    total = 0
    # Klik semua claim buttons SECEPAT MUNGKIN
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
                log(f"  [claim] bond #{total} clicked")
                time.sleep(2)
            except Exception as e:
                log(f"  [claim] err: {e}")
                break
        # Reload buat cari claim buttons baru
        try:
            page.reload(wait_until='load', timeout=30000)
            time.sleep(3)
        except Exception:
            break

    log(f"  [claim] total: {total}")
    return 'done' if total > 0 else 'skip'

# ═══════════════════════════════════════════
# POINTS
# ═══════════════════════════════════════════
def get_points(page):
    """Baca points dari halaman."""
    try:
        body = page.inner_text('body') or ''
        m = re.search(r'([\d,]+\.?\d*)\s*pts', body, re.IGNORECASE)
        if m:
            return float(m.group(1).replace(',', ''))
    except Exception:
        pass
    return 0

# ═══════════════════════════════════════════
# FAUCET
# ═══════════════════════════════════════════
def _find_visible(page, selectors):
    """Cari element visible dari list selectors."""
    for sel in selectors:
        try:
            loc = page.locator(sel)
            for i in range(loc.count()):
                el = loc.nth(i)
                if el.is_visible():
                    return el
        except Exception:
            pass
    return None

def try_claim_faucet(page):
    """Coba claim faucet BTC/frBTC."""
    try:
        log("  [faucet] buka setup page")
        page.goto(URL_SETUP, wait_until='load', timeout=30000)
        for _ in range(15):
            time.sleep(2)
            try:
                if page.locator('button:visible').count() > 0:
                    break
            except Exception:
                pass

        # Cari menu Link
        link_menu = _find_visible(page, [
            'a:has-text("Link")', 'button:has-text("Link")',
            '[role="tab"]:has-text("Link")', 'text="Link"',
        ])
        if not link_menu:
            log("  [faucet] menu Link ga ketemu")
            return 'not-available'
        log("  [faucet] klik Link")
        try:
            link_menu.scroll_into_view_if_needed()
            link_menu.click()
        except Exception as e:
            log(f"  [faucet] klik Link err: {e}")
            return 'error'
        time.sleep(3)

        # Klik wallet Linked
        wl = _find_visible(page, [
            'button:has-text("wallet Linked")', 'button:has-text("Wallet Linked")',
            'button:has-text("Linked")', 'text=/Linked/i',
        ])
        if wl:
            log("  [faucet] klik wallet Linked")
            try:
                wl.scroll_into_view_if_needed()
                wl.click()
                time.sleep(3)
            except Exception as e:
                log(f"  [faucet] err: {e}")
        else:
            log("  [faucet] wallet Linked ga ketemu (lanjut)")

        # Continue
        cont = _find_visible(page, [
            'button:has-text("Continue")', 'button:has-text("CONTINUE")',
        ])
        if cont:
            log("  [faucet] klik Continue")
            try:
                cont.scroll_into_view_if_needed()
                cont.click()
                time.sleep(4)
            except Exception as e:
                log(f"  [faucet] err: {e}")
        else:
            log("  [faucet] Continue ga ketemu (lanjut)")

        time.sleep(3)

        # Get BTC
        btc = _find_visible(page, [
            'button:has-text("Get BTC")', 'button:has-text("GET BTC")',
        ])
        if btc:
            try:
                dis = btc.get_attribute('disabled', timeout=1000)
                if dis is not None:
                    log("  [faucet] 'Get BTC' disabled")
                else:
                    btc.scroll_into_view_if_needed()
                    btc.click()
                    log("  [faucet] Get BTC clicked")
                    time.sleep(5)
            except Exception as e:
                log(f"  [faucet] Get BTC err: {e}")
        else:
            log("  [faucet] Get BTC ga ketemu")

        # Get frBTC
        frbtc = _find_visible(page, [
            'button:has-text("Get frBTC")', 'button:has-text("GET frBTC")',
        ])
        if frbtc:
            try:
                dis = frbtc.get_attribute('disabled', timeout=1000)
                if dis is not None:
                    log("  [faucet] 'Get frBTC' disabled")
                else:
                    frbtc.scroll_into_view_if_needed()
                    frbtc.click()
                    log("  [faucet] Get frBTC clicked")
                    time.sleep(5)
            except Exception as e:
                log(f"  [faucet] Get frBTC err: {e}")
        else:
            log("  [faucet] Get frBTC ga ketemu")

        # Check cooldown
        body = page.inner_text('body') or ''
        if re.search(r'cooldown|try again later|already claimed', body, re.IGNORECASE):
            log("  [faucet] cooldown")
            return 'cooldown'

        return 'claimed'
    except Exception as e:
        log(f"  [faucet] err: {e}")
        return 'error'

# ═══════════════════════════════════════════
# RETRY HELPER
# ═══════════════════════════════════════════
def retry_action(fn, tries=3, base_delay=5, label=""):
    """Retry action dengan exponential backoff."""
    for i in range(tries):
        if state['stop']:
            return 'stopped'
        try:
            r = fn()
            if r not in ('failed-3x', 'no-confirm', None):
                return r
        except Exception as e:
            log(f"  [retry:{label}] {i+1}/{tries} err: {e}")
        if i < tries - 1:
            delay = base_delay * (2 ** i) + random.uniform(0, 3)
            log(f"  [retry:{label}] {i+1}/{tries} -> retry in {delay:.0f}s")
            time.sleep(delay)
    return 'failed-3x'

def sleep_fixed(min_s, max_s, label=""):
    """Sleep with random duration."""
    d = random.uniform(min_s, max_s)
    log(f"  [jeda] {label}: {d:.0f}s")
    time.sleep(d)

# ═══════════════════════════════════════════
# FARMING SESSION
# ═══════════════════════════════════════════
def run_farming_session():
    """Main farming loop — sequential, no threads."""
    with Camoufox(headless=True) as browser:
        page = browser.new_page()

        page.goto(URL_STAKE, wait_until='load', timeout=30000)
        time.sleep(5)

        # Restore wallet
        if not restore_wallet(page):
            log("[FATAL] Wallet restore gagal!")
            notify("❌ Wallet restore gagal!")
            return False

        # Baca points awal
        time.sleep(5)
        pts = get_points(page)
        state['initial_points'] = pts
        state['last_notify_pts'] = pts
        log(f"[INIT] Points awal: {pts:.2f}")
        notify(f"🚀 <b>DOHM Farm v19-start</b>\nPoints: {pts:.2f}")

        next_faucet_time = time.time()
        faucet_result = None
        cycle_num = 0

        while not state['stop']:
            cycle_num += 1
            state['current_cycle'] = cycle_num

            # Auto-restart setiap 200 cycles (anti OOM)
            RESTART_EVERY = 200
            if cycle_num > 1 and cycle_num % RESTART_EVERY == 0:
                log(f"[RESTART] {cycle_num} cycles, restart...")
                notify(f"🔄 Auto-restart setelah {cycle_num} cycles")
                return False

            log(f"\n{'='*50}")
            log(f"CYCLE {cycle_num} | {datetime.now().strftime('%H:%M:%S')}")
            log(f"{'='*50}")

            # Wallet recovery check
            check_and_recover_wallet(page)

            pts = get_points(page)
            heartbeat(f"cycle={cycle_num} pts={pts:.2f}")

            # Cek target
            if pts >= POINTS_TARGET:
                msg = f"🎯 TARGET! {pts:.2f} / {POINTS_TARGET}"
                log(msg)
                notify(msg)
                state['stop'] = True
                return True

            # Notif tiap N pts
            if pts - state['last_notify_pts'] >= NOTIFY_EVERY_PTS:
                gained = pts - state['initial_points']
                notify(f"📈 <b>{pts:.2f} pts</b> (+{gained:.2f})\nCycle: {cycle_num}")
                state['last_notify_pts'] = pts

            log(f"  pts: {pts:.2f}")

            # ── FAUCET ──
            if FAUCET_ENABLED and time.time() >= next_faucet_time:
                log("  [faucet] checking...")
                faucet_result = retry_action(lambda: try_claim_faucet(page), tries=2, label="faucet")
                log(f"  [faucet] result: {faucet_result}")
                if faucet_result in ('claimed', 'cooldown'):
                    delay_hours = random.uniform(FAUCET_MIN_HOURS, FAUCET_MAX_HOURS)
                    next_faucet_time = time.time() + delay_hours * 3600
                    log(f"  [faucet] next check in {delay_hours:.1f}h")
                elif faucet_result == 'not-available':
                    next_faucet_time = time.time() + 3600
                else:
                    next_faucet_time = time.time() + 1800
                page.goto(URL_STAKE, wait_until='load', timeout=30000)
                time.sleep(3)

            # ── SKIP IF WALLET NEEDS FUND ──
            ws = check_wallet_status(page)
            if ws in ('needs_fund', 'needs_connect'):
                log(f"  [SKIP] Wallet {ws}")
                time.sleep(30)
                continue

            # ════════════════════════════════════════
            # SEQ: Stake → jeda → Unstake → jeda → Claim
            # ════════════════════════════════════════

            # ── STAKE ──
            log(f"  [SEQ 1/3] Stake {STAKE_AMOUNT} DOHM...")
            r1 = retry_action(lambda: do_stake(page, STAKE_AMOUNT), tries=3, label="stake")
            log(f"  -> {r1}")
            if r1 == 'disabled':
                log("  [!] Stake disabled, skip cycle")
                time.sleep(30)
                continue
            if r1 != 'done':
                log(f"  [!] stake gagal ({r1}), skip")
                notify(f"⚠️ Stake gagal cycle {cycle_num}: {r1}")
                time.sleep(60)
                continue

            sleep_fixed(JEDA_MIN, JEDA_MAX, "stake→unstake")

            # ── UNSTAKE ──
            log(f"  [SEQ 2/3] Unstake {UNSTAKE_AMOUNT} sDOHM...")
            r2 = retry_action(lambda: do_unstake(page, UNSTAKE_AMOUNT), tries=3, label="unstake")
            log(f"  -> {r2}")

            sleep_fixed(JEDA_MIN, JEDA_MAX, "unstake→claim")

            # ── CLAIM ──
            log("  [SEQ 3/3] Claim...")
            r3 = retry_action(lambda: do_claim(page), tries=2, label="claim")
            log(f"  -> {r3}")

            # Hitung cycle time
            sleep_fixed(JEDA_MIN, JEDA_MAX, "cycle end")

    return False  # supervisor will restart

# ═══════════════════════════════════════════
# SUPERVISOR
# ═══════════════════════════════════════════
def main():
    """Supervisor — auto-restart on crash."""
    # Lock file
    try:
        lock_fd = open(LOCK_FILE, 'w')
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("[LOCK] Script lain masih jalan. Exit.")
        sys.exit(1)

    log("=" * 50)
    log(f"DOHM Farm v19-fix | {datetime.now()}")
    log(f"Target: {POINTS_TARGET} | Mode: {'infinite' if MAX_CYCLES == 0 else MAX_CYCLES}")
    log("=" * 50)
    notify(f"🚀 <b>DOHM Farm v19-fix start</b>\nTarget: {POINTS_TARGET}")

    restart_count = 0
    while not state['stop']:
        try:
            run_farming_session()
        except KeyboardInterrupt:
            log("[EXIT] Manual stop")
            state['stop'] = True
            break
        except Exception as e:
            log(f"[CRASH] {e}")
            log(traceback.format_exc())
            notify(f"⚠️ Crash: {e}")
            restart_count += 1
            delay = min(300, 15 * restart_count)
            log(f"[RESTART] Restart #{restart_count} in {delay}s...")
            time.sleep(delay)

    log("[END] Farming selesai")
    notify("✅ Farming selesai")
    try:
        lock_fd.close()
    except Exception:
        pass

if __name__ == '__main__':
    main()
