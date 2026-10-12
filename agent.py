#!/usr/bin/env python3
"""sriai-distribution-agent: URL → ICP + ad → lead → scored draft (never sends)."""
from __future__ import annotations

import argparse, csv, json, os, re, sys
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

import neatlogs
import requests
from dotenv import load_dotenv
from openai import OpenAI

# --- config ---
ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
AUTO_APPROVE_THRESHOLD = int(os.getenv("AUTO_APPROVE_THRESHOLD", "80"))
MODEL, IST = "gpt-4o-mini", ZoneInfo("Asia/Kolkata")
IN_RATE, OUT_RATE = 0.15 / 1_000_000, 0.60 / 1_000_000
CRUST_CREDIT = 0.03  # list price per Person Search result (no premium fields)
CRUST_URL = "https://api.crustdata.com/person/search"
OUTBOX, COSTS, usage = ROOT / "outbox", ROOT / "costs.csv", {"credits": 0.0, "in": 0, "out": 0, "llm_usd": 0.0}
client = None


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(); self.bits, self.skip = [], 0
    def handle_starttag(self, t, _a):
        if t in ("script", "style", "noscript"): self.skip += 1
    def handle_endtag(self, t):
        if t in ("script", "style", "noscript") and self.skip: self.skip -= 1
    def handle_data(self, d):
        if not self.skip: self.bits.append(d)


def page_text(url: str) -> str:
    r = requests.get(url, timeout=25, headers={"User-Agent": "sriai-distribution-agent/1.0"})
    r.raise_for_status()
    p = _Text(); p.feed(r.text)
    return re.sub(r"\s+", " ", " ".join(p.bits)).strip()[:8000]


def rec(span, **attrs):
    for k, v in attrs.items():
        span.set_attribute(k, v)


def chat(name: str, user: str, system: str = "Reply with JSON only.") -> dict:
    with neatlogs.trace(name, kind="LLM") as span:
        r = client.chat.completions.create(
            model=MODEL, temperature=0.4, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        u = r.usage
        cost = u.prompt_tokens * IN_RATE + u.completion_tokens * OUT_RATE
        usage["in"] += u.prompt_tokens; usage["out"] += u.completion_tokens; usage["llm_usd"] += cost
        data = json.loads(r.choices[0].message.content)
        attrs = {"cost": round(cost, 6), "neatlogs.llm.model": MODEL}
        if "score" in data:
            sc = int(data["score"])
            attrs["score"] = sc
            attrs["decision"] = "draft_auto_approved" if sc >= AUTO_APPROVE_THRESHOLD else "draft_queued_for_review"
            if isinstance(data.get("breakdown"), dict):
                attrs["breakdown"] = json.dumps(data["breakdown"])
        rec(span, **attrs)
        return data


def find_str(obj, names):
    hits = []
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in names and isinstance(v, str) and v.strip():
                    hits.append((k, v.strip()))
                else:
                    walk(v)
        elif isinstance(x, list):
            for i in x:
                walk(i)
    walk(obj)
    rank = {n: i for i, n in enumerate(names)}
    hits.sort(key=lambda kv: rank.get(kv[0], 99))
    return hits[0][1] if hits else "unknown"


def pick_one(payload):
    if isinstance(payload, list) and payload: return payload[0]
    if isinstance(payload, dict):
        for v in payload.values():
            if isinstance(v, list) and v and isinstance(v[0], dict): return v[0]
        return payload
    raise RuntimeError("Crustdata returned no person object")


def crustdata(icp: dict, key: str) -> dict:
    role, industry, size = icp.get("role", ""), icp.get("industry", ""), icp.get("company_size", "")
    body = {
        "search": {"query": f"{role} in {industry} at {size} companies", "mode": "hybrid"},
        "fields": ["fit", "basic_profile"], "limit": 1,
    }
    with neatlogs.trace("crustdata.person.search", kind="TOOL") as span:
        rec(span, **{"neatlogs.tool.name": "crustdata_person_search", "neatlogs.tool.parameters": json.dumps(body)})
        r = requests.post(
            CRUST_URL, json=body, timeout=40,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                     "x-api-version": "2025-11-01", "Accept": "application/json"},
        )
        if r.status_code >= 400:
            rec(span, decision="failed", cost=0, error=r.text[:400])
            raise RuntimeError(f"Crustdata {r.status_code}: {r.text[:300]}")
        data = r.json()
        lead = pick_one(data)
        n = 1 if lead else 0
        usage["credits"] = n * CRUST_CREDIT
        rec(span, cost=usage["credits"], decision="ok", **{"output.value": json.dumps(lead)[:2000]})
        return lead


def next_monday_9(now: datetime) -> datetime:
    ahead = (7 - now.weekday()) % 7 or 7
    return (now + timedelta(days=ahead)).replace(hour=9, minute=0, second=0, microsecond=0)


def log_cost(url: str, decision: str, score: int):
    new = not COSTS.exists()
    with COSTS.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts", "url", "crustdata_credits", "llm_prompt_tokens", "llm_completion_tokens",
                        "llm_cost_usd", "total_cost", "score", "decision"])
        total = usage["credits"] + usage["llm_usd"]
        w.writerow([datetime.now(IST).isoformat(), url, usage["credits"], usage["in"], usage["out"],
                    round(usage["llm_usd"], 6), round(total, 6), score, decision])
    return usage["credits"] + usage["llm_usd"]


SCORE_PROMPT = (
    "Be strict. Score this first-touch email with this 100-point rubric:\n"
    "- personalisation (max 30): award 24-30 only if the draft uses a specific profile detail "
    "beyond job title and company (location, headline phrase, department). Mentioning country/city counts. "
    "Title+company only: max 12. Generic: max 6.\n"
    "- claims (max 25): every claim is true and taken only from the company website text\n"
    "- clarity (max 25): clear, under 70 words, exactly one soft question, no extra CTA\n"
    "- tone (max 20): respectful, not pushy. Deduct heavily for phrases like "
    "'try it free', 'try it free now', 'book a demo', 'act now', 'unlock', 'transform', "
    "'how can [product] assist you today'.\n"
    "Return JSON keys: score (int 0-100, sum of the four), "
    "breakdown {personalisation, claims, clarity, tone} (ints), reason (one line).\n"
    "Company website text:\n<<TEXT>>\nLead JSON:\n<<LEAD>>\nDraft:\n<<MSG>>"
)


def print_result(lead, msg, score, breakdown, reason, decision):
    name = find_str(lead, ("name", "professional_network_name"))
    title = find_str(lead, ("current_title", "title", "matched_title"))
    label = "auto-approved" if decision == "draft_auto_approved" else "queued for review"
    bd = breakdown if isinstance(breakdown, dict) else {}
    print(f"\n========== RESULT ==========\nLead name: {name}\nLead title: {title}\n\nDraft:\n{msg}")
    print(f"\nScore: {score}/100\n  personalisation (/30): {bd.get('personalisation', '?')}\n"
          f"  claims (/25): {bd.get('claims', '?')}\n  clarity (/25): {bd.get('clarity', '?')}\n"
          f"  tone (/20): {bd.get('tone', '?')}\nReason: {reason}\nDecision: {label}\nNever sent.")


def followups():
    now = datetime.now(IST)
    rows = [("24h", now + timedelta(hours=24)), ("36h", now + timedelta(hours=36)),
            ("72h", now + timedelta(hours=72)), ("next Monday 9am IST", next_monday_9(now))]
    print("\nFollow-up schedule (IST) — not sent")
    print(f"{'when':<24} {'at':<28}")
    print("-" * 52)
    for label, when in rows:
        print(f"{label:<24} {when.strftime('%a %Y-%m-%d %H:%M %Z')}")


def run(url: str, bad_key: bool):
    key = "invalid-bad-key" if bad_key else os.getenv("CRUSTDATA_API_KEY", "")
    if not bad_key and not key:
        raise RuntimeError("CRUSTDATA_API_KEY missing from .env")
    with neatlogs.trace("sriai-distribution-agent", kind="WORKFLOW") as root:
        try:
            text = page_text(url)
            icp_ad = chat("openai.icp_and_ad",
                f"Company site text:\n{text}\nReturn JSON keys: ideal_customer "
                "{role, industry, company_size}, suggested_meta_ad {headline, primary_text, audience}, "
                "founder_name (string or null: only a person this page clearly names as founder/co-founder; never invent).")
            icp, ad = icp_ad["ideal_customer"], icp_ad["suggested_meta_ad"]
            host = url.split("/")[2].removeprefix("www.")
            fname = (icp_ad.get("founder_name") or "").strip()
            sig = f"{fname}\nFounder, {host}" if fname else f"Founder, {host}"
            print("ICP:", json.dumps(icp), "\nMeta ad:", json.dumps(ad), "\nSender:\n" + sig)
            lead = crustdata(icp, key)
            detail = find_str(lead, ("full_location", "department", "sub_department", "headline"))
            drafted = chat("openai.draft_email",
                f"Company URL {url}. Company website text (only allowed source of claims):\n{text}\n"
                f"ICP {json.dumps(icp)}. Suggested ad {json.dumps(ad)}.\n"
                f"Raw lead JSON (do not assume field names):\n{json.dumps(lead)}\n"
                f"JSON keys: subject, body. Body MUST end with this exact signature block:\n{sig}\n"
                "Never sign as SriAI, Meghali, or Meghali Sachdeva. "
                f"The body MUST include this exact text: {detail}. "
                "Exactly one question mark: a soft curiosity question (not 'how can [product] help/assist you', not a free trial). "
                "No other call to action. Never write 'Try it free now', 'try it free', 'book a demo', or similar. "
                "Under 70 words including the signature. Never write 'I hope this finds you well'. "
                "Do not use any statistic or claim that is not in the company website text. "
                "Never claim we already spoke.")
            msg = drafted["body"]
            if detail != "unknown" and detail.lower() not in msg.lower():
                msg = msg.replace("\n", f"\nI noticed your profile mentions {detail}.\n", 1)
            scored = chat("openai.score_draft",
                SCORE_PROMPT.replace("<<TEXT>>", text).replace("<<LEAD>>", json.dumps(lead)).replace("<<MSG>>", msg))
            score = int(scored["score"])
            reason = scored.get("reason", "")
            breakdown = scored.get("breakdown") or {}
            if score >= AUTO_APPROVE_THRESHOLD:
                decision = "draft_auto_approved"
                save = True
            else:
                decision = "draft_queued_for_review"
                save = input("Queued for review. Write draft to outbox/? [y/n] ").strip().lower() == "y"
            if save:
                OUTBOX.mkdir(exist_ok=True)
                path = OUTBOX / f"{datetime.now(IST).strftime('%Y%m%dT%H%M%S')}_{decision}.json"
                path.write_text(json.dumps({"url": url, "ideal_customer": icp, "suggested_meta_ad": ad,
                                            "lead": lead, "message": msg, "score": score, "breakdown": breakdown,
                                            "reason": reason, "approval": decision, "sent": False}, indent=2))
                print(f"Saved {path} (never sent).")
            else:
                print("Skipped outbox write.")
            print_result(lead, msg, score, breakdown, reason, decision)
            total = log_cost(url, decision, score)
            rec(root, score=score, decision=decision, cost=round(total, 6))
            followups()
            print(f"Cost/lead: crustdata_credits={usage['credits']} llm_tokens={usage['in']+usage['out']} "
                  f"llm_usd={usage['llm_usd']:.6f} total={total:.6f} → {COSTS.name}")
        except Exception as e:
            rec(root, score=0, decision="failed", cost=round(usage["llm_usd"] + usage["credits"], 6), error=str(e)[:400])
            print(f"Run failed (traced): {e}", file=sys.stderr)
            raise


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="sriai-distribution-agent")
    ap.add_argument("url", help="Company website URL")
    ap.add_argument("--bad-key", action="store_true", help="Use a wrong Crustdata key to trace a failed run")
    args = ap.parse_args()
    if not os.getenv("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY missing from .env")
    neatlogs.init(api_key=os.getenv("NEATLOGS_API_KEY", ""), workflow_name="sriai-distribution-agent")
    client = neatlogs.wrap(OpenAI())
    code = 0
    try:
        run(args.url, args.bad_key)
    except Exception:
        code = 1
    finally:
        neatlogs.flush(); neatlogs.shutdown()
    sys.exit(code)
