import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

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
        r"(?i)api[_-]?key\\s*[:=]\\s*\\S+",
        r"(?i)bearer\\s+[A-Za-z0-9\\-\\._]+"
    ]
}


class ACEManager:
    """Lightweight ACE helper for Generator → Reflector → Curator."""

    def __init__(self, playbook_path: str = "playbook.json", guardrails_path: str = "guardrails.json"):
        self.playbook_path = Path(playbook_path)
        self.guardrails_path = Path(guardrails_path)
        self.playbook = self._load_playbook()
        self.guardrails = self._load_guardrails()
        self._migrate_playbook()

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
        # Trim to a reasonable length for prompts
        return cleaned[:2000]

    def _task_signature(self, task: str) -> List[str]:
        words = re.findall(r"[a-zA-Z0-9]+", task.lower())
        stop = {"the", "and", "or", "to", "for", "of", "a", "an", "in", "on", "at", "with", "by"}
        return [w for w in words if w and w not in stop][:12]

    def _similarity(self, sig_a: List[str], sig_b: List[str]) -> float:
        set_a, set_b = set(sig_a), set(sig_b)
        if not set_a or not set_b:
            return 0.0
        return len(set_a & set_b) / len(set_a | set_b)

    def _migrate_playbook(self) -> None:
        """Handle older formats (string tips → structured tips)."""
        tips = self.playbook.get("active_tips", [])
        migrated = []
        now = datetime.utcnow().isoformat() + "Z"
        if tips and isinstance(tips[0], str):
            for tip in tips:
                migrated.append({
                    "tip": tip,
                    "confidence": 0.6,
                    "task_signature": [],
                    "task": "",
                    "created_at": now,
                    "last_used": now
                })
            self.playbook["active_tips"] = migrated
            self._save_playbook()

    # --- Prompt overlay ---
    def prompt_overlay(self, current_task: str = "") -> str:
        tips = self._select_tips(current_task)
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
        return "\n".join(lines)

    # --- Reflection + Curation ---
    def record_run(
        self,
        task: str,
        outcome: str,
        actions: Optional[List[str]] = None,
        errors: Optional[List[str]] = None,
        preferences: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        actions = actions or []
        errors = errors or []
        preferences = preferences or []
        signature = self._task_signature(task)
        now_iso = datetime.utcnow().isoformat() + "Z"

        sanitized_entry = {
            "task": self._sanitize_text(task),
            "outcome": self._sanitize_text(outcome),
            "actions": [self._sanitize_text(a) for a in actions][-25:],
            "errors": [self._sanitize_text(e) for e in errors][-15:],
            "preferences": [self._sanitize_text(p) for p in preferences if p.strip()][:10],
            "timestamp": now_iso,
            "signature": signature,
        }

        self.playbook.setdefault("entries", []).append(sanitized_entry)
        curated_tips = self._curate_entry(sanitized_entry)
        self._update_active_tips(curated_tips)
        self._update_preferences(sanitized_entry.get("preferences", []))
        self._save_playbook()

        return {"tips": curated_tips, "preferences": sanitized_entry.get("preferences", [])}

    def _curate_entry(self, entry: Dict[str, Any]) -> List[Dict[str, Any]]:
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
                confidence=0.65
            ))

        if errors:
            for err in errors:
                tips.append(self._tip_obj(
                    f"Avoid repeating this failure for '{task}': {err[:180]}",
                    signature,
                    task,
                    confidence=0.7
                ))

        if actions:
            tips.append(self._tip_obj(
                f"Successful actions for '{task}': {', '.join(actions[-3:])[:280]}",
                signature,
                task,
                confidence=0.6
            ))

        if not tips:
            tips.append(self._tip_obj(
                f"Log kept for '{task}', no specific guidance yet.",
                signature,
                task,
                confidence=0.5
            ))

        for t in tips:
            t.setdefault("created_at", now)
            t.setdefault("last_used", now)
        return tips

    def _tip_obj(self, tip: str, signature: List[str], task: str, confidence: float) -> Dict[str, Any]:
        return {
            "tip": tip,
            "confidence": round(confidence, 2),
            "task_signature": signature,
            "task": task,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "last_used": datetime.utcnow().isoformat() + "Z",
        }

    def _update_active_tips(self, new_tips: List[Dict[str, Any]]) -> None:
        existing = self.playbook.get("active_tips", []) or []
        merged: List[Dict[str, Any]] = []
        index = {t["tip"]: t for t in existing if isinstance(t, dict) and "tip" in t}

        for tip_obj in new_tips:
            existing_tip = index.get(tip_obj["tip"])
            if existing_tip:
                # Boost confidence slightly and refresh signature/task/last_used
                updated_conf = min(1.0, (existing_tip.get("confidence", 0.6) * 0.7) + (tip_obj.get("confidence", 0.6) * 0.5))
                existing_tip.update({
                    "confidence": round(updated_conf, 2),
                    "task_signature": tip_obj.get("task_signature") or existing_tip.get("task_signature", []),
                    "task": tip_obj.get("task", existing_tip.get("task", "")),
                    "last_used": datetime.utcnow().isoformat() + "Z"
                })
            else:
                index[tip_obj["tip"]] = tip_obj

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
        # Keep top tips by confidence, drop anything very stale/low confidence
        tips = [t for t in tips if t.get("confidence", 0) >= 0.2]
        tips.sort(key=lambda t: t.get("confidence", 0), reverse=True)
        return tips[:20]

    def _update_preferences(self, new_prefs: List[str]) -> None:
        existing = self.playbook.get("preferences", []) or []
        for pref in new_prefs:
            if pref and pref not in existing:
                existing.append(pref)
        self.playbook["preferences"] = existing[:12]

    def _select_tips(self, current_task: str) -> List[Dict[str, Any]]:
        tips = self.playbook.get("active_tips", []) or []
        if not tips:
            return []
        signature = self._task_signature(current_task)
        scored = []
        for tip in tips:
            sig = tip.get("task_signature", [])
            sim = self._similarity(signature, sig) if signature else 0.0
            confidence = tip.get("confidence", 0.6)
            # Combine similarity and confidence (weighted)
            score = (sim * 0.6) + (confidence * 0.4)
            scored.append((score, tip))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = [t for s, t in scored if s >= 0.2][:8]
        if not top:
            top = [t for _, t in scored[:5]]
        # refresh last_used
        now_iso = datetime.utcnow().isoformat() + "Z"
        for t in top:
            t["last_used"] = now_iso
        self.playbook["active_tips"] = self._prune_tips(self._apply_decay(tips))
        self._save_playbook()
        return top


# Convenience singleton for modules that just need one manager
ace_manager = ACEManager()
