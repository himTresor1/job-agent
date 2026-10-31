"""AI-driven form field resolution.

The old approach hand-matched keywords per label ("if 'worked' in label and
'before' in label: ..."). That can't keep up with how many ways an ATS phrases
the same question. This module instead extracts every remaining field's real
structure (label, type, and — critically — its actual list of selectable
options) and asks the LLM to map field -> answer in one call, using ONLY facts
in the candidate's profile.

Same invariant as generator.py: never invent facts. Enforced two ways —
the prompt says so explicitly, and any select/combobox/radio answer that
doesn't exactly match one of that field's own enumerated options is dropped
in _validate_answers() rather than trusted.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

from .models import Job
from .profile import Profile

log = logging.getLogger(__name__)

# Fields the deterministic pre-fill already owns — never re-ask the LLM about these.
_SKIP_LABELS = {
    "first name", "last name", "full name", "name", "email", "phone",
    "resume", "resume/cv", "cv", "cover letter", "attach", "enter manually",
}

_AUTOCOMPLETE_IDS = {"country", "candidate-location"}


def _visible(loc) -> bool:
    try:
        return loc.first.is_visible()
    except Exception:
        return False


def _clean_label(txt: str) -> str:
    txt = re.sub(r"\s*\*$", "", txt or "").strip()
    txt = re.sub(r"\s*\(required\)$", "", txt, flags=re.I).strip()
    txt = re.sub(r"\s*\(optional\)$", "", txt, flags=re.I).strip()
    return txt


def _open_combobox_options(frame, input_el) -> list[str]:
    """Click a react-select control with no text typed to read its full option list."""
    kb = frame.keyboard if hasattr(frame, "keyboard") else frame.page.keyboard
    clicker = input_el
    try:
        ctrl = input_el.locator(
            "xpath=ancestor::*[contains(@class,'control') or contains(@class,'select')][1]"
        )
        if ctrl.count() > 0 and _visible(ctrl):
            clicker = ctrl.first
    except Exception:
        pass
    options: list[str] = []
    try:
        clicker.click(timeout=3000)
        frame.wait_for_timeout(500)
        oid = input_el.get_attribute("aria-controls") or input_el.get_attribute("aria-owns")
        menu = frame.locator(f'[id="{oid}"]') if oid else frame.locator('[role="listbox"]').first
        opts = menu.locator('[role="option"], .select__option') if menu.count() else frame.locator('[role="option"]')
        for j in range(min(opts.count(), 60)):
            t = (opts.nth(j).inner_text() or "").strip()
            if t and t not in options:
                options.append(t)
    except Exception as e:
        log.debug("field_resolver: could not open combobox options: %s", e)
    finally:
        try:
            kb.press("Escape")
            frame.wait_for_timeout(150)
        except Exception:
            pass
    return options


def _open_listbox_group_options(frame, group_el) -> list[str]:
    """Open a newer-Greenhouse-UI listbox widget (<div role="group"> wrapping an
    unlabeled <button aria-haspopup="listbox">) with no selection, read its full
    option list, then close without picking. Structurally different from the
    react-select pattern _open_combobox_options handles — there's no typing/
    filtering, just click the button, read the popped-open listbox, done."""
    kb = frame.keyboard if hasattr(frame, "keyboard") else frame.page.keyboard
    options: list[str] = []
    try:
        btn = group_el.locator('button[aria-haspopup="listbox"]').first
        if btn.count() == 0:
            return options
        btn.click(timeout=3000)
        frame.wait_for_timeout(500)
        list_id = btn.get_attribute("aria-controls") or ""
        menu = frame.locator(f'[id="{list_id}"]') if list_id else frame.locator('[role="listbox"]').first
        opts = menu.locator('[role="option"]') if menu.count() > 0 else frame.locator('[role="option"]')
        for j in range(min(opts.count(), 60)):
            t = (opts.nth(j).inner_text() or "").strip()
            if t and t not in options:
                options.append(t)
    except Exception as e:
        log.debug("field_resolver: could not open listbox-group options: %s", e)
    finally:
        try:
            kb.press("Escape")
            frame.wait_for_timeout(150)
        except Exception:
            pass
    return options


def extract_and_open_fields(frame) -> tuple[list[dict], dict[str, dict]]:
    """Read every unhandled field's label/type/options. Returns (llm_fields, locators).

    llm_fields is JSON-safe (sent to the model). locators maps each field id to
    the Playwright handles needed to fill it back in, kept out of the LLM call."""
    llm_fields: list[dict] = []
    locators: dict[str, dict] = {}
    seen_labels: set[str] = set()
    seen_control_ids: set[str] = set()
    idx = 0

    # Pass 1: fieldset+legend radio groups (LinkedIn/Lever-style accessible markup).
    try:
        for fs in frame.locator("fieldset").all():
            try:
                leg = fs.locator("legend").first
                if leg.count() == 0:
                    continue
                label = _clean_label(leg.inner_text() or "")
                if not label or len(label) < 3 or label.lower() in _SKIP_LABELS:
                    continue
                radios = fs.locator('input[type="radio"]')
                if radios.count() == 0:
                    continue
                opts = []
                for k in range(radios.count()):
                    rid = radios.nth(k).get_attribute("id") or ""
                    lbl = fs.locator(f'label[for="{rid}"]').first
                    t = (lbl.inner_text() or "").strip() if lbl.count() else ""
                    if t:
                        opts.append(t)
                    seen_control_ids.add(rid)
                if not opts or label.lower() in seen_labels:
                    continue
                seen_labels.add(label.lower())
                fid = f"f{idx}"; idx += 1
                required = bool(fs.locator("[aria-required='true']").count()) or "*" in (leg.inner_text() or "")
                llm_fields.append({"id": fid, "label": label, "kind": "radio",
                                    "options": opts, "required": required})
                locators[fid] = {"kind": "radio", "container": fs}
            except Exception:
                continue
    except Exception:
        pass

    # Pass 2: label -> single control (select, combobox, checkbox, text, textarea).
    try:
        labels = frame.locator("label")
        for i in range(labels.count()):
            try:
                lab = labels.nth(i)
                raw = (lab.inner_text() or "").strip()
                label = _clean_label(raw)
                ll = label.lower()
                if not label or len(label) < 2 or ll in _SKIP_LABELS or ll in seen_labels:
                    continue
                # NOTE: deliberately exact-match only, not substring — a substring
                # check here caught "Preferred First Name" and "How do you
                # pronounce your name?" as if they were the plain "First Name"/
                # "Name" contact fields already handled elsewhere, silently
                # dropping them (and everything after them was fine; this just
                # cost those two specific real screening questions).

                for_id = lab.get_attribute("for") or ""
                if for_id and for_id in seen_control_ids:
                    continue
                el = frame.locator(f'[id="{for_id}"]') if for_id else lab.locator("input, select, textarea").first
                if el.count() == 0:
                    continue
                el = el.first
                tag = el.evaluate("e => e.tagName.toLowerCase()")
                type_attr = (el.get_attribute("type") or "").lower()
                role = el.get_attribute("role") or ""
                required = (el.get_attribute("aria-required") == "true") or "*" in raw

                # Newer Greenhouse UI: label[for] sometimes points at a hidden
                # backing input (e.g. Location), with the real interactive
                # search box as a visible sibling within the label's OWN
                # immediate parent. Must resolve this BEFORE the visibility
                # gate below, since a type=hidden input always fails that
                # check. One level up only — two levels lands on a wider
                # wrapper shared with sibling fields (e.g. First Name), which
                # made the location search silently resolve to the wrong input.
                if tag == "input" and type_attr == "hidden":
                    container = lab.locator("xpath=..")
                    search_input = container.locator('input[aria-haspopup="listbox"]')
                    # This search input is itself styled invisible (same trick
                    # as react-select's real <input>) — the label being present
                    # and pointing here is proof enough the field is on-screen.
                    has_search_input = search_input.count() > 0
                    visible_input = search_input if has_search_input else container.locator('input:not([type="hidden"])')
                    if visible_input.count() > 0 and (has_search_input or _visible(visible_input.first)):
                        fid = f"f{idx}"; idx += 1
                        llm_fields.append({"id": fid, "label": label, "kind": "autocomplete",
                                            "options": [], "required": required})
                        locators[fid] = {"kind": "autocomplete", "input": visible_input.first}
                        seen_labels.add(ll)
                        if for_id:
                            seen_control_ids.add(for_id)
                    continue

                # React-select's real <input> is deliberately invisible (the
                # styled control div is what's shown) — only require visibility
                # for elements that are actually rendered directly, or this
                # drops every combobox/demographic dropdown on the form.
                if role != "combobox" and not _visible(el):
                    continue

                fid = f"f{idx}"
                if tag == "select":
                    opts = []
                    for opt in el.locator("option").all():
                        t = (opt.inner_text() or "").strip()
                        if t and t.lower() not in ("select an option", "please select", "-select-", ""):
                            opts.append(t)
                    if not opts:
                        continue
                    idx += 1
                    llm_fields.append({"id": fid, "label": label, "kind": "select",
                                        "options": opts, "required": required})
                    locators[fid] = {"kind": "select", "input": el}
                elif role == "combobox" or type_attr == "text" and "select__input" in (el.get_attribute("class") or ""):
                    if for_id in _AUTOCOMPLETE_IDS or any(k in ll for k in ("location", "city")):
                        idx += 1
                        llm_fields.append({"id": fid, "label": label, "kind": "autocomplete",
                                            "options": [], "required": required})
                        locators[fid] = {"kind": "autocomplete", "input": el}
                    else:
                        opts = _open_combobox_options(frame, el)
                        idx += 1
                        llm_fields.append({"id": fid, "label": label, "kind": "combobox",
                                            "options": opts, "required": required})
                        locators[fid] = {"kind": "combobox", "input": el}
                elif tag == "div" and role == "group" and el.locator('button[aria-haspopup="listbox"]').count() > 0:
                    opts = _open_listbox_group_options(frame, el)
                    if not opts:
                        continue
                    idx += 1
                    llm_fields.append({"id": fid, "label": label, "kind": "listbox_group",
                                        "options": opts, "required": required})
                    locators[fid] = {"kind": "listbox_group", "group": el}
                elif type_attr == "checkbox":
                    idx += 1
                    llm_fields.append({"id": fid, "label": label, "kind": "checkbox",
                                        "options": ["check", "leave_unchecked"], "required": required})
                    locators[fid] = {"kind": "checkbox", "input": el}
                elif type_attr == "radio":
                    continue  # handled in pass 1; bare radios with no fieldset are rare and skipped
                elif tag == "textarea":
                    idx += 1
                    llm_fields.append({"id": fid, "label": label, "kind": "textarea",
                                        "options": [], "required": required})
                    locators[fid] = {"kind": "textarea", "input": el}
                elif tag == "input" and type_attr not in ("file", "hidden", "submit", "button"):
                    idx += 1
                    llm_fields.append({"id": fid, "label": label, "kind": "text",
                                        "options": [], "required": required})
                    locators[fid] = {"kind": "text", "input": el}
                else:
                    continue
                seen_labels.add(ll)
                if for_id:
                    seen_control_ids.add(for_id)
            except Exception:
                continue
    except Exception as e:
        log.warning("field_resolver: extraction failed: %s", e)

    return llm_fields, locators


def validate_answers(fields: list[dict], answers: dict) -> dict:
    """Drop any answer that isn't grounded in that field's own option list.

    This is the actual enforcement of 'never invent an option' — not just a
    prompt instruction, which a model can still ignore under pressure."""
    by_id = {f["id"]: f for f in fields}
    clean = {}
    for fid, ans in (answers or {}).items():
        f = by_id.get(fid)
        if f is None or ans is None:
            continue
        ans = str(ans).strip()
        if not ans:
            continue
        if f["kind"] in ("select", "combobox", "radio", "listbox_group"):
            match = next((o for o in f["options"] if o.strip().lower() == ans.lower()), None)
            if not match:
                match = next((o for o in f["options"] if ans.lower() in o.lower() or o.lower() in ans.lower()), None)
            if not match:
                log.warning("field_resolver: dropping hallucinated option %r for %r", ans, f["label"])
                continue
            clean[fid] = match
        elif f["kind"] == "checkbox":
            clean[fid] = "check" if ans.lower().startswith("check") else "leave_unchecked"
        else:
            clean[fid] = ans
    return clean


def _build_prompt(fields: list[dict], profile: Profile, job: Job) -> str:
    return (
        "You are filling in the remaining fields of a job application form on behalf "
        "of the candidate below. Answer ONLY from facts in the candidate profile.\n\n"
        "RULES (safety-critical, follow exactly):\n"
        "- Never invent an employer, date, title, certificate, number, or skill not in the profile.\n"
        "- Facts can be EMBEDDED in a longer profile string — e.g. a GPA written inside the "
        "education \"degree\" text like \"BSc (Hons) ... (4.2/5 CGPA)\". Extract and use it; "
        "don't treat it as missing just because it isn't its own field.\n"
        "- For kind=select/combobox/radio/listbox_group: your answer MUST be copied verbatim "
        "from that field's own \"options\" list. If none of the options is truthful/answerable "
        "from the profile, respond null.\n"
        "- For a self-assessment/experience-level scale (e.g. \"how would you describe your "
        "level of experience with X\"): pick the option best supported by the profile's actual "
        "skills/experience entries — don't default to null just because there's no single "
        "sentence stating the tier outright. Inferring a level FROM listed real skills/work is "
        "not the same as inventing a fact.\n"
        "- For kind=checkbox: respond exactly \"check\" or \"leave_unchecked\".\n"
        "- For kind=autocomplete (a location/country search box): respond with a short place "
        "name from the candidate's location field, e.g. the city or country.\n"
        "- For kind=text/textarea reflective/motivational questions (e.g. \"how has X changed "
        "your work\", \"why this role\"): write a short, honest, first-person answer that "
        "references specific real skills or experience entries from the profile — this is "
        "expected, not optional, whenever the profile contains ANY relevant skill or experience "
        "to draw on. Only respond null if the profile truly has nothing relevant to the question's "
        "subject at all.\n"
        "- If a field is not required and the profile has zero relevant information for it, "
        "respond null rather than guess.\n\n"
        f"=== CANDIDATE PROFILE ===\n{json.dumps(profile.__dict__, indent=2)}\n\n"
        f"=== JOB ===\nTitle: {job.title}\nCompany: {job.company}\nLocation: {job.location}\n\n"
        f"=== FIELDS TO ANSWER ===\n{json.dumps(fields, indent=2)}\n\n"
        "Respond with ONLY valid JSON, no markdown fences:\n"
        '{"answers": {"<field id>": "<value or null>", ...}}\n'
    )


def _parse_json(text: str) -> dict:
    try:
        return json.loads(text.strip())
    except Exception:
        cleaned = re.sub(r"```(?:json)?", "", text).strip()
        return json.loads(cleaned)


def _call_openai_compatible(url: str, model: str, prompt: str, api_key: str) -> dict:
    import requests
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }
    backoffs = (5, 15, 30)
    for attempt in range(len(backoffs) + 1):
        res = requests.post(url, json=payload, headers=headers, timeout=30)
        if res.status_code == 429 and attempt < len(backoffs):
            time.sleep(backoffs[attempt])
            continue
        res.raise_for_status()
        return _parse_json(res.json()["choices"][0]["message"]["content"])
    return {}


def _call_groq(prompt: str, api_key: str) -> dict:
    return _call_openai_compatible(
        "https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile", prompt, api_key
    )


def _call_chatgpt(prompt: str, api_key: str) -> dict:
    return _call_openai_compatible(
        "https://api.openai.com/v1/chat/completions", "gpt-4o-mini", prompt, api_key
    )


def _call_gemini(prompt: str, api_key: str) -> dict:
    import google.generativeai as genai
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.5-flash")
    resp = model.generate_content(
        prompt, generation_config=genai.types.GenerationConfig(response_mime_type="application/json")
    )
    return _parse_json(resp.text)


def _call_anthropic(prompt: str, api_key: str) -> dict:
    from anthropic import Anthropic
    client = Anthropic(api_key=api_key)
    resp = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return _parse_json(text)


def resolve_answers(fields: list[dict], profile: Profile, job: Job) -> dict[str, str]:
    """One LLM call: field list -> {id: answer}. Returns {} on any failure (caller falls back)."""
    if not fields:
        return {}
    prompt = _build_prompt(fields, profile, job)
    raw: dict = {}
    try:
        if os.environ.get("GROQ_API_KEY"):
            raw = _call_groq(prompt, os.environ["GROQ_API_KEY"])
        elif os.environ.get("OPENAI_API_KEY"):
            raw = _call_chatgpt(prompt, os.environ["OPENAI_API_KEY"])
        elif os.environ.get("GEMINI_API_KEY"):
            raw = _call_gemini(prompt, os.environ["GEMINI_API_KEY"])
        elif os.environ.get("ANTHROPIC_API_KEY"):
            raw = _call_anthropic(prompt, os.environ["ANTHROPIC_API_KEY"])
    except Exception as e:
        log.warning("field_resolver: LLM call failed, falling back to heuristics: %s", e)
        return {}
    return validate_answers(fields, raw.get("answers") or {})
