"""Read a live form into a list of fields, decide each value, fill it, and read it back.

Label-driven and ATS-agnostic: every visible input, select, textarea, radio group, checkbox and
combobox is described by its label text, and values come from the profile, the answer bank or
(for non-legal, profile-derivable questions only) the LLM.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.config import Profile
from agent.gates import FieldPlan
from agent.prepare import answers

log = logging.getLogger(__name__)

# Runs in the page. Returns one entry per logical field (radio groups merged).
EXTRACT_JS = r"""
() => {
  const visible = el => {
    const s = getComputedStyle(el); const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && (r.width > 0 || r.height > 0 || el.type === 'file');
  };
  const text = el => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const labelFor = el => {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return text(l); }
    const by = el.getAttribute('aria-labelledby');
    if (by) return by.split(/\s+/).map(id => text(document.getElementById(id))).join(' ');
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    const wrap = el.closest('label'); if (wrap) return text(wrap);
    const fs = el.closest('fieldset'); if (fs && fs.querySelector('legend')) return text(fs.querySelector('legend'));
    const box = el.closest('.field, .application-question, .form-group, [class*="field"], [class*="question"]');
    if (box) { const l = box.querySelector('label, .label, .application-label, legend'); if (l) return text(l); }
    return el.placeholder || el.name || '';
  };
  // Upload widgets label the button ("Attach"), not the field; use the widget's own heading instead.
  const fileLabel = el => {
    const own = labelFor(el);
    if (!/^(attach|upload|choose file|browse|select file)?$/i.test(own.replace(/\s*\*\s*$/, '').trim())) return own;
    const box = el.closest('[class*="upload"], [class*="file"], .field, fieldset, [role="group"]');
    const head = box && box.querySelector('[id*="label"], .label, legend, label:not([for]), [class*="label"]');
    const t = head ? text(head) : '';
    return t && !/^(attach|upload)/i.test(t) ? t : (el.id || el.name || own).replace(/[_-]/g, ' ');
  };
  // Custom dropdowns keep a hidden twin input for validation; only the visible combobox is a field.
  const hiddenTwin = el => el.getAttribute('aria-hidden') === 'true' || getComputedStyle(el).opacity === '0'
    || (el.tabIndex === -1 && !el.getAttribute('role'));
  const selectorFor = (el, i) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    if (el.name) return `${el.tagName.toLowerCase()}[name="${el.name}"]`;
    el.setAttribute('data-agent-idx', String(i)); return `[data-agent-idx="${i}"]`;
  };
  const out = []; const groups = {};
  const els = Array.from(document.querySelectorAll('input, select, textarea'));
  els.forEach((el, i) => {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type)) return;
    if (!visible(el) || el.disabled) return;
    if (type !== 'file' && hiddenTwin(el)) return;
    let label = type === 'file' ? fileLabel(el) : labelFor(el);
    const required = el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label);
    label = label.replace(/\s*\*\s*$/, '').trim();
    if (type === 'radio' || (type === 'checkbox' && el.name && document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`).length > 1)) {
      const fs = el.closest('fieldset, [role="radiogroup"], [role="group"], .field, [class*="question"]');
      const groupLabel = fs ? text(fs.querySelector('legend, label, .label, [class*="label"]')) : el.name;
      const key = el.name || groupLabel;
      if (!groups[key]) {
        groups[key] = {kind: type === 'radio' ? 'radio' : 'checkbox_group', label: groupLabel.replace(/\s*\*\s*$/, ''),
                       required: required || /\*\s*$/.test(groupLabel), selector: `input[name="${el.name}"]`, options: []};
        out.push(groups[key]);
      }
      groups[key].options.push(labelFor(el).replace(/\s*\*\s*$/, '') || el.value);
      groups[key].required = groups[key].required || required;
      return;
    }
    const entry = {kind: el.getAttribute('role') === 'combobox' ? 'combobox' : (el.tagName === 'SELECT' ? 'select' : (el.tagName === 'TEXTAREA' ? 'textarea' : type)),
                   label, required, selector: selectorFor(el, i), options: null, accept: el.getAttribute('accept') || ''};
    if (el.tagName === 'SELECT') entry.options = Array.from(el.options).map(o => o.text.trim()).filter(t => t && !/^(select|choose|please select|--)/i.test(t));
    out.push(entry);
  });
  const combos = new Set(out.filter(e => e.kind === 'combobox').map(e => e.label));
  return out.filter(e => !(e.kind === 'input' && combos.has(e.label)));
}
"""

ERRORS_JS = r"""
() => Array.from(document.querySelectorAll('[aria-invalid="true"], .error, .field-error, .error-message, [class*="error"]'))
  .filter(el => { const s = getComputedStyle(el); return s.display !== 'none' && s.visibility !== 'hidden' && (el.innerText || '').trim(); })
  .map(el => (el.innerText || '').trim().slice(0, 120)).slice(0, 10)
"""

RESUME_RE = re.compile(r"resume|cv\b|curriculum", re.IGNORECASE)
COVER_RE = re.compile(
    r"cover\s*letter|cover\s*note|additional information|why .*(us|company|role)", re.IGNORECASE
)


@dataclass
class Field:
    kind: str
    label: str
    required: bool
    selector: str
    options: list[str] | None = None
    accept: str = ""

    @classmethod
    def from_js(cls, raw: dict[str, Any]) -> Field:
        return cls(
            kind=raw["kind"], label=raw["label"] or "", required=bool(raw["required"]), selector=raw["selector"],
            options=raw.get("options"), accept=raw.get("accept") or "",
        )  # fmt: skip


@dataclass
class Planned:
    field: Field
    plan: FieldPlan
    uploads: list[Path] = field(default_factory=list)


def plan_fields(
    fields: list[Field],
    conn: sqlite3.Connection,
    profile: Profile,
    client,
    resume: Path | None,
    cover_text: str,
    job_id: int | None,
    company: str | None = None,
) -> list[Planned]:
    out = []
    for f in fields:
        cat = answers.category(f.label)
        value, source, uploads = None, None, []
        if f.kind == "file":
            if RESUME_RE.search(f.label) or (f.required and not COVER_RE.search(f.label)):
                if resume:
                    value, source, uploads = resume.name, "file", [resume]
            elif COVER_RE.search(f.label) and f.required:
                cover_file = resume.parent / "cover.txt" if resume else None
                if cover_file and cover_file.exists():
                    value, source, uploads = cover_file.name, "file", [cover_file]
        elif f.kind == "textarea" and COVER_RE.search(f.label):
            value, source = cover_text, "profile"
        elif f.kind == "checkbox" and not f.options:
            # Single checkboxes are consents ("I agree to the privacy policy"). Tick only if required.
            if f.required:
                value, source = "yes", "profile"
        else:
            hit = answers.lookup(conn, f.label, f.options)
            ruled = answers.rule_answer(f.label, profile, company) if not hit and cat != "legal" else None
            ruled = answers.match_option(ruled, f.options or []) if ruled else None
            if hit:
                value, source = hit.value, hit.source
            elif ruled:
                value, source = ruled, "profile"
            elif cat != "legal" and client is not None and f.required:
                try:
                    derived = answers.derive(client, profile, f.label, f.options)
                except Exception as exc:  # quota etc.: treat as unanswerable this time
                    log.warning("derive failed: %s", type(exc).__name__)
                    derived = None
                if derived:
                    value, source = derived.value, "llm"
                    answers.upsert(conn, f.label, derived.value, "factual", approved=False, overwrite=False)
            if value is None and f.required:
                answers.record_pending(conn, f.label, job_id)
        out.append(Planned(f, FieldPlan(f.label, f.required, f.kind, value, source, cat, f.options), uploads))
    return out


def fill(page, planned: list[Planned]) -> None:
    for item in planned:
        f, value = item.field, item.plan.value
        if value is None:
            continue
        loc = page.locator(f.selector).first
        if f.kind == "file":
            loc.set_input_files([str(p) for p in item.uploads])
        elif f.kind == "select":
            loc.select_option(label=value)
        elif f.kind in ("radio", "checkbox_group"):
            _check_option(page, f, value)
        elif f.kind == "checkbox":
            loc.check()
        elif f.kind == "combobox":
            _choose_combobox(page, loc, f, value)
        else:
            loc.fill(value)


def _choose_combobox(page, loc, f: Field, value: str) -> None:
    """Open the dropdown, read its real options, pick the one that matches exactly (yes/no aware)."""
    loc.click()
    page.wait_for_timeout(300)
    options = page.locator("[role=option]:visible")
    if not options.count():
        loc.fill(value)  # searchable dropdowns load options as you type
        page.wait_for_timeout(500)
    texts = [t.strip() for t in options.all_inner_texts()]
    choice = answers.match_option(value, texts)
    if choice is None:
        page.keyboard.press("Escape")
        raise ValueError(f"no option matching the answer for {f.label}")
    options.nth(texts.index(choice)).click(timeout=5000)


def _check_option(page, f: Field, value: str) -> None:
    for radio in page.locator(f.selector).all():
        label = radio.evaluate(
            "el => (el.labels && el.labels[0] ? el.labels[0].innerText : el.value || '').trim()"
        )
        if label.strip().lower() == value.strip().lower():
            radio.check()
            return
    raise ValueError(f"option not found for {f.label}")


def read_back(page, planned: list[Planned]) -> dict[str, str]:
    actual: dict[str, str] = {}
    for item in planned:
        f = item.field
        if item.plan.value is None:
            continue
        loc = page.locator(f.selector).first
        if f.kind == "file":
            actual[f.label] = loc.evaluate("el => el.files && el.files[0] ? el.files[0].name : ''")
        elif f.kind == "select":
            actual[f.label] = loc.evaluate(
                "el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text.trim() : ''"
            )
        elif f.kind in ("radio", "checkbox_group"):
            actual[f.label] = page.evaluate(
                "sel => { const el = Array.from(document.querySelectorAll(sel)).find(r => r.checked);"
                " return el ? ((el.labels && el.labels[0] ? el.labels[0].innerText : el.value) || '').trim() : ''; }",
                f.selector,
            )
        elif f.kind == "checkbox":
            actual[f.label] = "yes" if loc.is_checked() else ""
        elif f.kind == "combobox":
            actual[f.label] = loc.evaluate(
                "el => { const box = el.closest('[class*=container], [class*=select]') || el.parentElement;"
                " return (box.innerText || el.value || '').trim(); }"
            )
            if item.plan.value.lower() in actual[f.label].lower():
                actual[f.label] = item.plan.value
        else:
            actual[f.label] = loc.input_value()
    return actual


def intended(planned: list[Planned]) -> dict[str, str]:
    return {p.field.label: p.plan.value for p in planned if p.plan.value is not None}
