
import json
import re
import os
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # Optional

DEFAULT_PLAYBOOK = {
    "entries": [],
    "active_tips": [],
    "preferences": []
}

DEFAULT_GUARDRAILS = {
    "notes": [
        "Do not store secrets, tokens, session data, emails, or URLs with query parameters.",
        "Summaries should focus on behaviors and steps, not raw scraped content.",
        "User-supplied preferences are allowed and should be preserved."
    ],
    "never_store_terms": ["password", "token", "cookie", "session", "secret", "api_key"],
    "redact_patterns": [
        r"sk-[A-Za-z0-9]{20,}",
        r"(?i)api[_-]?key\s*[:=]\s*\S+",
        r"(?i)bearer\s+[A-Za-z0-9\-\._]+"
    ]
}


class ACEManager:
    """Lightweight ACE helper for Generator → Reflector → Curator with structured telemetry."""

    def __init__(self, playbook_path: str = "playbook.json", guardrails_path: str = "guardrails.json"):
        self.playbook_path = Path(playbook_path)
        self.guardrails_path = Path(guardrails_path)
        self.playbook = self._load_playbook()
        self.guardrails = self._load_guardrails()
        self._migrate_playbook()
        self._client = None
        if OpenAI and os.getenv("OPENAI_API_KEY"):
            try:
                self._client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            except Exception:
                self._client = None

    # --- Load / Save ---
    def _load_playbook(self) -> Dict[str, Any]:
        if not self.playbook_path.exists():
            self.playbook_path.write_text(json.dumps(DEFAULT_PLAYBOOK, indent=2), encoding="utf-8")
            return dict(DEFAULT_PLAYBOOK)
        try:
            data = json.loads(self.playbook_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        for key, default_value in DEFAULT_PLAYBOOK.items():
            data.setdefault(key, default_value if not isinstance(default_value, list) else list(default_value))
        return data

    def _load_guardrails(self) -> Dict[str, Any]:
        if not self.guardrails_path.exists():
            self.guardrails_path.write_text(json.dumps(DEFAULT_GUARDRAILS, indent=2), encoding="utf-8")
            return dict(DEFAULT_GUARDRAILS)
        try:
            return json.loads(self.guardrails_path.read_text(encoding="utf-8"))
        except Exception:
            return dict(DEFAULT_GUARDRAILS)

    def _save_playbook(self) -> None:
        self.playbook_path.write_text(json.dumps(self.playbook, indent=2), encoding="utf-8")

    # --- Guardrail helpers ---
    def _sanitize_text(self, text: str) -> str:
        if not text:
            return ""
        cleaned = text
        for pattern in self.guardrails.get("redact_patterns", []):
            cleaned = re.sub(pattern, "[REDACTED]", cleaned)
        for term in self.guardrails.get("never_store_terms", []):
            if term.lower() in cleaned.lower():
                cleaned = cleaned.replace(term, "[REDACTED]")
        return cleaned[:2000]

    def _sanitize_dict(self, data: Dict[str, Any]) -> Dict[str, Any]:
        cleaned: Dict[str, Any] = {}
        for k, v in data.items():
            if v is None:
                continue
            if isinstance(v, str):
                cleaned[k] = self._sanitize_text(v)[:500]
            elif isinstance(v, (int, float, bool)):
                cleaned[k] = v
            else:
                cleaned[k] = str(v)[:200]
        return cleaned

    def _task_signature(self, task: str) -> List[str]:
        words = re.findall(r"[a-zA-Z0-9]+", task.lower())
        stop = {"the", "and", "or", "to", "for", "of", "a", "an", "in", "on", "at", "with", "by"}
        return [w for w in words if w and w not in stop][:12]

    def _similarity(self, sig_a: List[str], sig_b: List[str]) -> float:
        set_a, set_b = set(sig_a), set(sig_b)
        if not set_a or not set_b:
            return 0.0
        return len(set_a & set_b) / len(set_a | set_b)

    def _tip_id(self, tip: str, domain: str) -> str:
        return hashlib.sha256(f"{domain}::{tip}".encode()).hexdigest()[:12]

    def _migrate_playbook(self) -> None:
        """Handle older formats (string tips → structured tips, add counts/domain/id)."""
        tips = self.playbook.get("active_tips", [])
        now = datetime.utcnow().isoformat() + "Z"
        migrated: List[Dict[str, Any]] = []
        for tip in tips:
            if isinstance(tip, str):
                migrated.append({
                    "tip": tip,
                    "confidence": 0.6,
                    "task_signature": [],
                    "task": "",
                    "created_at": now,
                    "last_used": now,
                    "domain": "default",
                    "helpful_count": 0,
                    "harmful_count": 0,
                    "id": self._tip_id(tip, "default"),
                })
            elif isinstance(tip, dict):
                tip.setdefault("confidence", 0.6)
                tip.setdefault("task_signature", [])
                tip.setdefault("task", "")
                tip.setdefault("created_at", now)
                tip.setdefault("last_used", now)
                tip.setdefault("domain", "default")
                tip.setdefault("helpful_count", 0)
                tip.setdefault("harmful_count", 0)
                tip.setdefault("id", self._tip_id(tip.get("tip", ""), tip.get("domain", "default")))
                migrated.append(tip)
        if migrated:
            self.playbook["active_tips"] = migrated
            self._save_playbook()

    # --- Prompt overlay ---
    def prompt_overlay(self, current_task: str, domain: str = "default") -> (str, List[str]):
        tips = self._select_tips(current_task, domain=domain)
        preferences = self.playbook.get("preferences", []) or []
        lines: List[str] = []
        if tips:
            lines.append("ACE curated tips (recent, matched):")
            lines.extend([f"- {t['tip']}" for t in tips])
        if preferences:
            lines.append("User preferences to respect:")
            lines.extend([f"- {p}" for p in preferences[:5]])
        guardrail_notes = self.guardrails.get("notes", [])
        if guardrail_notes:
            lines.append("Guardrails:")
            lines.extend([f"- {note}" for note in guardrail_notes])
        overlay = "\n".join(lines)
        used_tip_ids = [t.get("id") or self._tip_id(t.get("tip", ""), t.get("domain", domain)) for t in tips]
        return overlay, used_tip_ids

    # --- Reflection + Curation ---
    def record_run(
        self,
        task: str,
        outcome: str,
        actions: Optional[List[Any]] = None,
        errors: Optional[List[str]] = None,
        preferences: Optional[List[str]] = None,
        goal_status: str = "unknown",
        reason_for_status: str = "",
        answer_relevance_score: Optional[float] = None,
        used_tip_ids: Optional[List[str]] = None,
        domain: str = "default",
    ) -> Dict[str, Any]:
        actions = actions or []
        errors = errors or []
        preferences = preferences or []
        used_tip_ids = used_tip_ids or []
        signature = self._task_signature(task)
        now_iso = datetime.utcnow().isoformat() + "Z"

        if answer_relevance_score is None:
            if goal_status == "success":
                answer_relevance_score = 0.9 if not errors else 0.75
            elif goal_status == "partial":
                answer_relevance_score = 0.6
            elif goal_status in {"failed", "blocked"}:
                answer_relevance_score = 0.2
            else:
                answer_relevance_score = 0.5

        sanitized_actions: List[Dict[str, Any]] = []
        for act in actions:
            if isinstance(act, dict):
                sanitized_actions.append(self._sanitize_dict(act))
            elif isinstance(act, str):
                sanitized_actions.append({"message": self._sanitize_text(act)})
            else:
                sanitized_actions.append({"message": str(act)[:300]})

        sanitized_entry = {
            "task": self._sanitize_text(task),
            "outcome": self._sanitize_text(outcome),
            "actions": sanitized_actions[-50:],
            "errors": [self._sanitize_text(e) for e in errors][-25:],
            "preferences": [self._sanitize_text(p) for p in preferences if p.strip()][:10],
            "timestamp": now_iso,
            "signature": signature,
            "goal_status": goal_status,
            "reason_for_status": reason_for_status,
            "answer_relevance_score": round(float(answer_relevance_score or 0.0), 2),
            "used_tip_ids": used_tip_ids,
            "domain": domain,
        }

        self.playbook.setdefault("entries", []).append(sanitized_entry)
        curated_tips = self._curate_entry(sanitized_entry, domain)
        reflected_tips = self._reflect_on_entry(sanitized_entry) if self._client else []
        all_new_tips = curated_tips + reflected_tips
        self._update_active_tips(all_new_tips)
        self._update_preferences(sanitized_entry.get("preferences", []))
        self._update_tip_feedback(used_tip_ids, goal_status)
        self._save_playbook()

        return {"tips": all_new_tips, "preferences": sanitized_entry.get("preferences", [])}

    def _curate_entry(self, entry: Dict[str, Any], domain: str) -> List[Dict[str, Any]]:
        tips: List[Dict[str, Any]] = []
        task = entry.get("task", "").strip()
        outcome = entry.get("outcome", "").strip()
        errors = entry.get("errors", [])
        actions = entry.get("actions", [])
        signature = entry.get("signature", [])
        now = datetime.utcnow().isoformat() + "Z"

        if outcome:
            tips.append(self._tip_obj(
                f"When tackling '{task}', keep the last observed outcome in mind: {outcome[:280]}",
                signature,
                task,
                confidence=0.65,
                domain=domain
            ))

        if errors:
            for err in errors:
                tips.append(self._tip_obj(
                    f"Avoid repeating this failure for '{task}': {err[:180]}",
                    signature,
                    task,
                    confidence=0.7,
                    domain=domain
                ))

        if actions:
            def summarize_action(act: Any) -> str:
                if isinstance(act, dict):
                    tool = act.get("tool", "tool")
                    res = act.get("result_type", "ok")
                    err = act.get("error_category", "none")
                    return f"{tool} [{res}/{err}]"
                return str(act)[:80]
            summary = ", ".join(summarize_action(a) for a in actions[-3:])
            tips.append(self._tip_obj(
                f"Recent actions for '{task}': {summary[:280]}",
                signature,
                task,
                confidence=0.6,
                domain=domain
            ))

        if not tips:
            tips.append(self._tip_obj(
                f"Log kept for '{task}', no specific guidance yet.",
                signature,
                task,
                confidence=0.5,
                domain=domain
            ))

        for t in tips:
            t.setdefault("created_at", now)
            t.setdefault("last_used", now)
        return tips

    def _tip_obj(self, tip: str, signature: List[str], task: str, confidence: float, domain: str) -> Dict[str, Any]:
        tip_id = self._tip_id(tip, domain)
        return {
            "id": tip_id,
            "tip": tip,
            "confidence": round(confidence, 2),
            "task_signature": signature,
            "task": task,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "last_used": datetime.utcnow().isoformat() + "Z",
            "domain": domain,
            "helpful_count": 0,
            "harmful_count": 0,
        }

    def _update_active_tips(self, new_tips: List[Dict[str, Any]]) -> None:
        existing = self.playbook.get("active_tips", []) or []
        index = {t.get("id") or self._tip_id(t.get("tip", ""), t.get("domain", "default")): t for t in existing if isinstance(t, dict)}

        for tip_obj in new_tips:
            tid = tip_obj.get("id") or self._tip_id(tip_obj.get("tip", ""), tip_obj.get("domain", "default"))
            tip_obj["id"] = tid
            existing_tip = index.get(tid)
            if existing_tip:
                updated_conf = min(1.0, (existing_tip.get("confidence", 0.6) * 0.7) + (tip_obj.get("confidence", 0.6) * 0.5))
                existing_tip.update({
                    "confidence": round(updated_conf, 2),
                    "task_signature": tip_obj.get("task_signature") or existing_tip.get("task_signature", []),
                    "task": tip_obj.get("task", existing_tip.get("task", "")),
                    "last_used": datetime.utcnow().isoformat() + "Z",
                    "domain": tip_obj.get("domain", existing_tip.get("domain", "default")),
                })
                existing_tip.setdefault("helpful_count", 0)
                existing_tip.setdefault("harmful_count", 0)
            else:
                index[tid] = tip_obj

        merged = list(index.values())
        merged = self._apply_decay(merged)
        merged = self._prune_tips(merged)
        self.playbook["active_tips"] = merged

    def _apply_decay(self, tips: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        decayed = []
        now = datetime.utcnow()
        for tip in tips:
            last_used = tip.get("last_used", tip.get("created_at"))
            try:
                last_dt = datetime.fromisoformat(last_used.replace("Z", ""))
            except Exception:
                last_dt = now
            days_old = (now - last_dt).days
            decay_factor = max(0.5, 1 - (0.02 * days_old))  # 2% per day, floor at 0.5
            tip["confidence"] = round(max(0.1, tip.get("confidence", 0.6) * decay_factor), 2)
            decayed.append(tip)
        return decayed

    def _prune_tips(self, tips: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        tips = [t for t in tips if t.get("confidence", 0) >= 0.2]
        tips.sort(key=lambda t: t.get("confidence", 0), reverse=True)
        return tips[:20]

    def _update_preferences(self, new_prefs: List[str]) -> None:
        existing = self.playbook.get("preferences", []) or []
        for pref in new_prefs:
            if pref and pref not in existing:
                existing.append(pref)
        self.playbook["preferences"] = existing[:12]

    def _select_tips(self, current_task: str, domain: str = "default") -> List[Dict[str, Any]]:
        tips = self.playbook.get("active_tips", []) or []
        if not tips:
            return []
        signature = self._task_signature(current_task)
        scored = []
        for tip in tips:
            tip_domain = tip.get("domain", "default")
            if tip_domain not in {domain, "global"}:
                continue
            sig = tip.get("task_signature", [])
            sim = self._similarity(signature, sig) if signature else 0.0
            confidence = tip.get("confidence", 0.6)
            score = (sim * 0.6) + (confidence * 0.4)
            scored.append((score, tip))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = [t for s, t in scored if s >= 0.2][:8]
        if not top:
            top = [t for _, t in scored[:5]]
        now_iso = datetime.utcnow().isoformat() + "Z"
        for t in top:
            t["last_used"] = now_iso
        self.playbook["active_tips"] = self._prune_tips(self._apply_decay(self.playbook.get("active_tips", [])))
        self._save_playbook()
        return top

    def _update_tip_feedback(self, used_tip_ids: List[str], goal_status: str) -> None:
        if not used_tip_ids:
            return
        updated = False
        for tip in self.playbook.get("active_tips", []):
            tid = tip.get("id") or self._tip_id(tip.get("tip", ""), tip.get("domain", "default"))
            if tid not in used_tip_ids:
                continue
            tip.setdefault("helpful_count", 0)
            tip.setdefault("harmful_count", 0)
            if goal_status == "success":
                tip["helpful_count"] += 1
                tip["confidence"] = min(1.0, tip.get("confidence", 0.6) + 0.05)
            elif goal_status in {"failed", "blocked"}:
                tip["harmful_count"] += 1
                tip["confidence"] = max(0.1, tip.get("confidence", 0.6) - 0.05)
            updated = True
        if updated:
            self._save_playbook()

    def _reflect_on_entry(self, entry: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not self._client:
            return []
        try:
            prompt = (
                "You are an analyst generating 1-3 concise tips to improve future runs of a task. "
                "Return bullet tips only, no preamble."
            )
            user_content = json.dumps({
                "task": entry.get("task", ""),
                "outcome": entry.get("outcome", ""),
                "errors": entry.get("errors", []),
                "actions": entry.get("actions", [])
            })
            resp = self._client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_content}
                ]
            )
            text = resp.choices[0].message.content or ""
            tips = []
            for line in text.splitlines():
                line = line.strip("- •• ").strip()
                if not line:
                    continue
                tips.append(self._tip_obj(line[:280], entry.get("signature", []), entry.get("task", ""), confidence=0.6, domain=entry.get("domain", "default")))
                if len(tips) >= 3:
                    break
            return tips
        except Exception:
            return []


# Convenience singleton for modules that just need one manager
ace_manager = ACEManager()