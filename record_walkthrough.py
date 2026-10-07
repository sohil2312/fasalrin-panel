"""
Record a hand walkthrough on fasalrin.gov.in (the user drives, the script only watches) so a new automation can
copy the exact steps: clicks, typed / picked values, URL changes, popups, and the form's fields at each step.

    .venv\\Scripts\\python record_walkthrough.py fresh_walkthrough.log [--profile .pw_profile]

Privacy: nothing is recorded on the login page; password / captcha / OTP / mobile fields are never recorded;
12-digit numbers (Aadhaar) are masked to the last 4 digits. The log stays on this PC (*.log is git-ignored).
Close the browser window to stop.
"""

from __future__ import annotations

import argparse
import json
import re
import time

from playwright.sync_api import sync_playwright

BASE = "https://fasalrin.gov.in"

JS = r"""
(() => {
  if (window.__wtOn) return; window.__wtOn = true;
  const SECRET = /pass|captcha|otp|mobile|mpin|pin\b/i;
  const onLogin = () => /login/i.test(location.pathname);
  const mask = v => String(v ?? "").replace(/\b(\d{4})[\s-]?(\d{4})[\s-]?(\d{4})\b/g, "XXXX-XXXX-$3");
  const labelOf = el => {
    if (!el) return "";
    const id = el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (id) return id.innerText.trim();
    const ff = el.closest("mat-form-field, .form-group, .mb-3, .col, td, div");
    const l = ff && ff.querySelector("label, mat-label, .label, span.lbl");
    return (l ? l.innerText : (el.getAttribute("aria-label") || el.placeholder || el.name || el.getAttribute("formcontrolname") || "")).trim().slice(0, 80);
  };
  const desc = el => ({tag: el.tagName.toLowerCase(), id: el.id || "", name: el.name || "",
    fc: el.getAttribute("formcontrolname") || "", type: el.type || "", ph: el.placeholder || "",
    cls: (el.className && el.className.baseVal === undefined ? el.className : "").toString().slice(0, 60),
    label: labelOf(el), text: (el.innerText || el.value || "").trim().slice(0, 80)});
  const secret = el => onLogin() || SECRET.test([el.type, el.name, el.id, el.placeholder, el.getAttribute("formcontrolname"), labelOf(el)].join(" "));
  const send = o => { try { window.__wtsend(JSON.stringify({...o, url: location.pathname})); } catch (e) {} };
  const snapshot = why => {
    if (onLogin()) return;
    const f = [...document.querySelectorAll("input, select, textarea, mat-select")].filter(e => e.offsetParent !== null).slice(0, 120).map(e => {
      const d = desc(e);
      let v = e.tagName === "SELECT" ? (e.selectedOptions[0] || {}).text : (e.tagName === "MAT-SELECT" ? e.innerText : e.value);
      if (e.type === "checkbox" || e.type === "radio") v = e.checked;
      d.value = secret(e) ? "<hidden>" : mask(v).slice(0, 80);
      if (e.tagName === "SELECT") d.options = [...e.options].map(o => o.text.trim()).slice(0, 40);
      return d;
    });
    const btns = [...document.querySelectorAll("button, a.btn, input[type=submit]")].filter(e => e.offsetParent !== null)
      .map(b => (b.innerText || b.value || "").trim()).filter(Boolean).slice(0, 40);
    send({ev: "snapshot", why, fields: f, buttons: btns, heading: mask((document.querySelector("h1,h2,h3,h4,.card-header") || {}).innerText || "").slice(0, 120)});
  };
  document.addEventListener("click", e => {
    const el = e.target.closest("button, a, input, select, option, mat-option, li, td, label, span, div") || e.target;
    if (onLogin()) return;
    send({ev: "click", el: {...desc(el), text: secret(el) ? "<hidden>" : mask(desc(el).text)}});
    if (/^(button|a)$/i.test(el.tagName) || el.closest("button")) setTimeout(() => snapshot("after click: " + mask(desc(el).text).slice(0, 40)), 1500);
  }, true);
  document.addEventListener("change", e => {
    const el = e.target; if (onLogin()) return;
    let v = el.tagName === "SELECT" ? (el.selectedOptions[0] || {}).text : (el.type === "checkbox" || el.type === "radio" ? el.checked : el.value);
    send({ev: "change", el: desc(el), value: secret(el) ? "<hidden>" : mask(v).slice(0, 120)});
  }, true);
  let lastDlg = "";
  new MutationObserver(() => {
    const d = [...document.querySelectorAll("mat-dialog-container, .modal.show, .swal2-popup, .mat-snack-bar-container, .toast, [role=alert], .alert")].filter(x => x.offsetParent !== null).map(x => x.innerText.trim()).join(" | ");
    if (d && d !== lastDlg && !onLogin()) { lastDlg = d; send({ev: "popup", text: mask(d).slice(0, 600)}); }
  }).observe(document.documentElement, {childList: true, subtree: true});
  let lastUrl = location.href;
  setInterval(() => { if (location.href !== lastUrl) { lastUrl = location.href; send({ev: "nav"}); setTimeout(() => snapshot("page"), 2500); } }, 500);
})();
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--profile", default=".pw_profile")
    a = ap.parse_args()
    out = open(a.log, "a", encoding="utf-8")

    def rec(_src, payload):
        try:
            o = json.loads(payload)
        except ValueError:
            return
        o["t"] = time.strftime("%H:%M:%S")
        out.write(json.dumps(o, ensure_ascii=False) + "\n")
        out.flush()

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(a.profile, headless=False, viewport=None, args=["--start-maximized"])
        ctx.expose_binding("__wtsend", rec)
        ctx.add_init_script(JS)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(BASE, wait_until="domcontentloaded")
        rec(None, json.dumps({"ev": "start", "url": "/"}))
        print("Recording. Log in, do the sample entries, then close the browser window.", flush=True)
        try:
            while ctx.pages:
                ctx.pages[0].wait_for_timeout(1000)
        except Exception:
            pass
        rec(None, json.dumps({"ev": "stop", "url": ""}))


if __name__ == "__main__":
    main()
