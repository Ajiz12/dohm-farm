#!/usr/bin/env python3
"""
DOHM Farming v12
24/7 auto-farming: stake → unstake → claim bonds → repeat.
Features: auto retry, Telegram notif, auto update, heartbeat, supervisor.
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
import json
import subprocess
from datetime import datetime

from camoufox.sync_api import Camoufox

# ═══════════════════════════════════════════
# CONFIG (all from env vars, no hardcoded secrets)
# ═══════════════════════════════════════════
WALLET_PASSWORD = os.environ.get('WALLET_PASSWORD')
SEED_PHRASE     = os.environ.get('SEED_PHRASE')
if not WALLET_PASSWORD or not SEED_PHRASE:
    print("[FATAL] WALLET_PASSWORD & SEED_PHRASE wajib di-set.")
    sys.exit(1)

POINTS_TARGET  = float(os.environ.get('POINTS_TARGET', '1000'))
MAX_CYCLES     = int(os.environ.get('MAX_CYCLES', '0'))           # 0 = infinite 24/7
WAIT_MINUTES   = int(os.environ.get('WAIT_MINUTES', '10'))
WAIT_JITTER    = int(os.environ.get('WAIT_JITTER', '60'))
STAKE_AMOUNT   = float(os.environ.get('STAKE_AMOUNT', '0.2'))
UNSTAKE_AMOUNT = float(os.environ.get('UNSTAKE_AMOUNT', '0.1'))

# Telegram Notif
TELEGRAM_BOT_TOKEN  = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID    = os.environ.get('TELEGRAM_CHAT_ID', '')
NOTIFY_ON_HEARTBEAT = int(os.environ.get('NOTIFY_ON_HEARTBEAT', '0'))  # tiap N cycle, 0=off

# Auto Update
AUTO_UPDATE              = int(os.environ.get('AUTO_UPDATE', '0'))
UPDATE_URL               = os.environ.get('UPDATE_URL', '')
UPDATE_CHECK_EVERY_CYCLE = int(os.environ.get('UPDATE_CHECK_EVERY_CYCLE', '5'))

# Paths
URL_STAKE     = 'https://testnet.dohm.finance/app/stake'
URL_PORTFOLIO = 'https://testnet.dohm.finance/app/portfolio'
LOCK_FILE     = '/tmp/dohm_farm.lock'
LOG_FILE      = '/tmp/dohm_farm.log'
HEARTBEAT_FILE= '/tmp/dohm_farm.heartbeat'
SCRIPT_PATH   = os.path.abspath(__file__)

MAX_RESTARTS = 0  # 0 = infinite restart


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
    """Kirim notif ke Telegram. Fallback ke console kalau env ga di-set."""
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
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"[NOTIF] gagal kirim: {e}")


def heartbeat(payload: str):
    try:
        with open(HEARTBEAT_FILE, 'w') as f:
            f.write(f"{datetime.now().isoformat()} | {payload}\n")
    except Exception:
        pass


# ═══════════════════════════════════════════
# AUTO UPDATE
# ═══════════════════════════════════════════
def file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


def check_and_update():
    """Cek UPDATE_URL, kalau beda hash → replace file + restart diri sendiri."""
    if not AUTO_UPDATE or not UPDATE_URL:
        return False
    try:
        log(f"[UPDATE] cek {UPDATE_URL}")
        req = urllib.request.Request(UPDATE_URL, headers={'User-Agent': 'dohm-farm'})
        with urllib.request.urlopen(req, timeout=20) as r:
            remote = r.read()
            local = open(SCRIPT_PATH, 'rb').read()
            remote_hash = hashlib.sha256(remote).hexdigest()
            local_hash  = hashlib.sha256(local).hexdigest()
            if remote_hash == local_hash:
                log("[UPDATE] sudah versi terbaru")
                return False
            log(f"[UPDATE] versi baru terdeteksi ({local_hash[:8]} -> {remote_hash[:8]})")
            backup = SCRIPT_PATH + '.bak'
            with open(backup, 'wb') as f:
                f.write(local)
            with open(SCRIPT_PATH, 'wb') as f:
                f.write(remote)
            os.chmod(SCRIPT_PATH, 0o755)
            notify(f"🔄 <b>Auto-update</b>\nDOHM Farm diupdate ke versi baru.\nHash: <code>{remote_hash[:12]}</code>\nRestart otomatis...")
            log("[UPDATE] restarting...")
            time.sleep(2)
            os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        log(f"[UPDATE] error: {e}")
    return False


# ═══════════════════════════════════════════
# LOCK (cegah duplikasi instance)
# ═══════════════════════════════════════════
def acquire_lock():
    f = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        log("[LOCK] Script lain masih jalan. Exit.")
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
        m = re.findall(r'(0x[a-fA-F0-9]{64})', txt)
        if m:
            return m[-1]
        m = re.findall(r'([a-f0-9]{8,}\.[a-f0-9]{4,})', txt)
        if m:
            return m[-1]
        m = re.findall(r'\b([a-f0-9]{64})\b', txt)
        return m[-1] if m else None
    except Exception:
        return None


def read_points_no_nav(page):
    try:
        txt = page.inner_text('body') or ''
        m = re.search(r'(\d+\.?\d*)\s*pts', txt, re.IGNORECASE)
        if not m:
            m = re.search(r'[Pp]oints?\s*[:=]?\s*(\d+\.?\d*)', txt)
        return float(m.group(1)) if m else None
    except Exception:
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
    connect_btn = page.locator('button:has-text("Connect wallet"):visible')
    if connect_btn.count() > 0:
        return 'needs_connect'
    try:
        body = page.inner_text('body')
    except Exception:
        return 'needs_connect'
    if re.search(r'\bbcrt1q[a-z0-9]{20,}\b', body) or \
       re.search(r'\b0x[a-fA-F0-9]{40}\b', body):
        return 'connected'
    for label in ['Stake DOHM', 'Unstake DOHM']:
        btn = page.locator(f'button:has-text("{label}"):not([disabled]):visible')
        if btn.count() > 0:
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
        unlock = page.locator('button:has-text("Unlock"):visible')
        if unlock.count() > 0:
            unlock.first.click()
            time.sleep(4)
            log("  [wallet] unlocked")


def restore_wallet(page):
    log("  [wallet] clicking Connect wallet...")
    try:
        page.locator('button:has-text("Connect wallet"):visible').first.click()
    except Exception as e:
        log(f"  [wallet] connect click err: {e}")
        return False
    time.sleep(2)
    restore = page.locator('button:has-text("Restore from recovery phrase"):visible')
    if restore.count() == 0:
        log("  [wallet] ERROR: Restore button not found")
        return False
    restore.first.click()
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
    status = check_wallet_status(page)
    log(f"  [wallet] status after restore: {status}")
    return status == 'connected'


def ensure_wallet(page):
    status = check_wallet_status(page)
    log(f"  [wallet] current status: {status}")
    if status == 'connected':
        return True
    if status == 'needs_unlock':
        unlock_wallet(page)
        return check_wallet_status(page) == 'connected'
    if restore_wallet(page):
        return True
    unlock_wallet(page)
    return check_wallet_status(page) == 'connected'


# ═══════════════════════════════════════════
# FORM HELPERS
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
    active = page.locator('button[aria-selected="true"]').first
    if active.count() > 0:
        return active.inner_text().strip()
    return 'none'


def click_confirm_sign(page, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        btn = page.locator('button:has-text("Confirm & sign"):not([disabled]):visible')
        if btn.count() > 0:
            try:
                btn.first.scroll_into_view_if_needed()
                btn.first.click()
                time.sleep(4)
                return 'confirmed'
            except Exception as e:
                log(f"  [confirm] click err: {e}")
        if get_tx_hash(page):
            return 'confirmed'
        time.sleep(1)
    return 'no-confirm'


def retry_action(fn, tries=3, base_delay=5, label=""):
    """Generic retry wrapper dengan exponential backoff."""
    for i in range(tries):
        try:
            r = fn()
            if r not in ('failed-3x', 'no-confirm', None):
                return r
            log(f"  [retry:{label}] attempt {i + 1}/{tries} -> {r}")
        except Exception as e:
            log(f"  [retry:{label}] attempt {i + 1}/{tries} error: {e}")
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
            tabs = page.locator('button:has-text("Stake"):visible')
            if tabs.count() > 0:
                tabs.first.click()
            time.sleep(3)
            active = verify_tab(page)
        if active != 'Stake':
            log(f"  retry {attempt + 1}: wrong tab '{active}'")
            time.sleep(3)
            continue
        val = fill_amount(page, amount)
        log(f"  input: '{val}'")
        submit = page.locator('button:has-text("Stake DOHM"):not([disabled]):visible')
        if submit.count() > 0:
            submit.first.scroll_into_view_if_needed()
            submit.first.click()
            time.sleep(2)
            conf = click_confirm_sign(page)
            tx = get_tx_hash(page)
            log(f"  stake tx: {tx} | confirm: {conf}")
            return 'done' if conf == 'confirmed' else conf
        log(f"  attempt {attempt + 1}: no-submit")
        time.sleep(2)
    return 'failed-3x'


def get_sohm_balance(page):
    try:
        txt = page.inner_text('body') or ''
        m = re.search(r'(\d+\.?\d*)\s*sDOHM', txt, re.IGNORECASE)
        if m:
            return float(m.group(1))
        m = re.search(r'[Bb]alance\s+(\d+\.?\d*)\s*sDOHM', txt)
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
        tabs = page.locator('button:has-text("Unstake"):visible')
        if tabs.count() > 0:
            tabs.first.click()
        time.sleep(3)
    sohm_bal = get_sohm_balance(page)
    if sohm_bal <= 0:
        log(f"  no sDOHM to unstake (balance={sohm_bal})")
        return 'skip'
    if amount < sohm_bal:
        unstake_amount = amount
    else:
        unstake_amount = round(max(sohm_bal - 0.001, 0), 4)
    if unstake_amount <= 0:
        return 'skip'
    log(f"  sDOHM balance: {sohm_bal:.4f}, unstaking: {unstake_amount}")
    for attempt in range(3):
        active = verify_tab(page)
        if active != 'Unstake':
            tabs = page.locator('button:has-text("Unstake"):visible')
            if tabs.count() > 0:
                tabs.first.click()
            time.sleep(3)
            active = verify_tab(page)
        if active != 'Unstake':
            time.sleep(3)
            continue
        val = fill_amount(page, unstake_amount)
        log(f"  input: '{val}'")
        time.sleep(1)
        submit = page.locator('button:has-text("Unstake DOHM"):visible')
        if submit.count() > 0:
            is_disabled = submit.first.get_attribute('disabled', timeout=1000)
            if is_disabled is None:
                submit.first.scroll_into_view_if_needed()
                submit.first.click()
                time.sleep(2)
                conf = click_confirm_sign(page)
                tx = get_tx_hash(page)
                log(f"  unstake tx: {tx} | confirm: {conf}")
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

    total_claimed = 0
    for round_num in range(10):
        n = page.locator('button:has-text("Claim"):not([disabled]):visible').count()
        if n == 0:
            break
        log(f"  [claim] round {round_num + 1}: {n} tombol keliatan")
        for _ in range(n):
            btn = page.locator('button:has-text("Claim"):not([disabled]):visible').first
            if btn.count() == 0:
                break
            try:
                btn.scroll_into_view_if_needed()
                btn.click()
                total_claimed += 1
                time.sleep(8)
            except Exception as e:
                log(f"  [claim] err: {e}")
                break
        try:
            page.reload(wait_until='load', timeout=30000)
            time.sleep(8)
        except Exception:
            break
    log(f"  [claim] total: {total_claimed}")
    return 'done' if total_claimed > 0 else 'skip'


# ═══════════════════════════════════════════
# CYCLE RUNNER
# ═══════════════════════════════════════════
def run_farming_session():
    log(f"[INIT] DOHM Farm v12 | {datetime.now()}")
    log(f"[INIT] Flow: Stake {STAKE_AMOUNT} -> Wait {WAIT_MINUTES}m -> "
        f"Unstake {UNSTAKE_AMOUNT} -> Wait {WAIT_MINUTES}m -> Claim -> Wait {WAIT_MINUTES}m -> Repeat")
    notify(
        f"🚀 <b>DOHM Farm v12 start</b>\n"
        f"Target: {POINTS_TARGET} pts\n"
        f"Mode: {'infinite 24/7' if MAX_CYCLES == 0 else f'{MAX_CYCLES} cycles'}"
    )

    with Camoufox(headless=True) as browser:
        page = browser.new_page()
        page.goto(URL_STAKE, wait_until='load', timeout=30000)
        time.sleep(5)
        ensure_wallet(page)

        initial_points = get_points(page)
        log(f"[INIT] Points: {initial_points:.2f} | Target: {POINTS_TARGET}")
        cycle_start = time.time()
        cycle_num = 0

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

            log(f"  pts: {pts:.2f}")

            # ── AUTO UPDATE CHECK ──
            if AUTO_UPDATE and cycle_num % UPDATE_CHECK_EVERY_CYCLE == 0:
                check_and_update()

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

            # ── HEARTBEAT NOTIF ──
            if NOTIFY_ON_HEARTBEAT and cycle_num % NOTIFY_ON_HEARTBEAT == 0:
                pts_now = get_points(page)
                notify(
                    f"💓 Cycle {cycle_num} selesai\n"
                    f"pts={pts_now:.2f} | elapsed={(time.time() - cycle_start) / 3600:.2f}h"
                )

            elapsed = (time.time() - cycle_start) / 3600
            pts_now = get_points(page)
            log(f"  cycle {cycle_num} done. pts={pts_now:.2f} | elapsed={elapsed:.2f}h | "
                f"gained={pts_now - initial_points:.2f}")


# ═══════════════════════════════════════════
# SUPERVISOR
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
                notify("✅ <b>DOHM Farm selesai</b>\nTarget tercapai / max cycles reached.")
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
            if MAX_RESTARTS > 0 and restart_count >= MAX_RESTARTS:
                log("[SUPERVISOR] Max restarts reached. Exit.")
                notify(f"💀 DOHM Farm crash {restart_count}x, max reached. Exit.")
                break
            notify(f"⚠️ DOHM Farm crash #{restart_count}: {e}")
            delay = min(300, 30 * restart_count)
            log(f"[SUPERVISOR] Restart in {delay}s...")
            time.sleep(delay)
