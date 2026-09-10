"""Stateful session tracking and iterative context routing for ProjectBrain.

A Session represents an agent's ongoing exploration of a codebase for a
specific task.  It tracks which files have been read, which have been modified,
and maintains a *frontier* — a priority queue of the next files the agent should
read, ranked by graph distance and task relevance.

This is the "Brain" in Project Brain: instead of dumping 40 files at once,
Brain routes the agent through the codebase step by step, the way a human brain
navigates through a problem.
"""
from __future__ import annotations

import heapq
import time
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .brain import ProjectBrain


@dataclass
class FrontierItem:
    """A file in the exploration frontier with priority metadata."""
    rel_path: str
    priority: float        # lower = higher priority (heapq is min-heap)
    reason: str            # why Brain is suggesting this file
    via: str               # which read file led here (the "because you read X" link)
    graph_distance: int    # hops from nearest read file

    def __lt__(self, other: "FrontierItem") -> bool:
        return self.priority < other.priority

    def to_dict(self) -> dict:
        return {
            "rel_path": self.rel_path,
            "priority": round(self.priority, 3),
            "reason": self.reason,
            "via": self.via,
            "graph_distance": self.graph_distance,
        }


@dataclass
class Session:
    """An agent's codebase exploration session."""
    session_id: str
    task: str
    brain: "ProjectBrain"

    # State tracking
    read_files: set[str] = field(default_factory=set)
    modified_files: dict[str, str] = field(default_factory=dict)  # path → summary
    created_at: float = field(default_factory=time.time)

    # Frontier (priority queue of next files to read)
    _frontier_heap: list[FrontierItem] = field(default_factory=list)
    _frontier_set: set[str] = field(default_factory=set)  # quick membership check
    _initial_files: list[str] = field(default_factory=list)

    def next_file(self, count: int = 1) -> list[dict]:
        """Pop the next N files from the frontier.

        Returns a list of dicts with rel_path, priority, reason, via,
        graph_distance for each suggested file.  Files already read are
        silently skipped.
        """
        results: list[dict] = []
        while self._frontier_heap and len(results) < count:
            item = heapq.heappop(self._frontier_heap)
            self._frontier_set.discard(item.rel_path)
            if item.rel_path not in self.read_files:
                results.append(item.to_dict())
        return results

    def mark_read(self, rel_path: str) -> dict:
        """Mark a file as read and expand the frontier from it.

        Returns the updated frontier (top 5 suggestions).
        """
        rel_path = _normalize(rel_path)
        self.read_files.add(rel_path)
        self._frontier_set.discard(rel_path)

        # Expand frontier: find files connected to the read file
        self._expand_frontier_from(rel_path)

        return {
            "marked": rel_path,
            "read_count": len(self.read_files),
            "frontier_size": len(self._frontier_heap),
            "next_suggestions": self.next_file(count=0),  # peek, don't pop
            "completeness": self._estimate_completeness(),
        }

    def mark_modified(self, rel_path: str, summary: str = "") -> dict:
        """Mark a file as modified and track downstream impact.

        Returns files that import the modified file (potential impact zone).
        """
        rel_path = _normalize(rel_path)
        self.modified_files[rel_path] = summary
        # Also mark as read — you've seen it if you modified it
        self.read_files.add(rel_path)

        # Find files that import this one — they might need updating too
        importers = self.brain._imported_by.get(rel_path, [])
        unread_importers = [f for f in importers if f not in self.read_files]

        # Add unread importers to frontier with high priority
        for imp in unread_importers:
            self._add_to_frontier(
                imp,
                priority=0.5,  # High priority
                reason=f"imports modified file '{rel_path}' — may need updates",
                via=rel_path,
                distance=1,
            )

        return {
            "marked_modified": rel_path,
            "summary": summary,
            "impact_zone": importers,
            "unread_impacted": unread_importers,
            "frontier_size": len(self._frontier_heap),
        }

    def peek_frontier(self, count: int = 5) -> list[dict]:
        """Preview the top N frontier items without removing them."""
        # Collect items, skipping already-read files
        items: list[dict] = []
        temp: list[FrontierItem] = []
        while self._frontier_heap and len(items) < count:
            item = heapq.heappop(self._frontier_heap)
            temp.append(item)
            if item.rel_path not in self.read_files:
                items.append(item.to_dict())
        # Put them back
        for item in temp:
            heapq.heappush(self._frontier_heap, item)
        return items

    def status(self) -> dict:
        """Return session status summary."""
        return {
            "session_id": self.session_id,
            "task": self.task,
            "read_files": sorted(self.read_files),
            "read_count": len(self.read_files),
            "modified_files": dict(self.modified_files),
            "modified_count": len(self.modified_files),
            "frontier_size": sum(
                1 for item in self._frontier_heap
                if item.rel_path not in self.read_files
            ),
            "next_suggestions": self.peek_frontier(5),
            "completeness": self._estimate_completeness(),
            "initial_files": self._initial_files,
        }

    # ── Internal ────────────────────────────────────────────────────────────

    def _expand_frontier_from(self, rel_path: str) -> None:
        """BFS one hop outward from a read file through imports/calls."""
        # Files this file imports (outgoing deps)
        targets = self.brain._import_targets.get(rel_path, [])
        for target in targets:
            if target not in self.read_files:
                # Build a reason from the import relationship
                reason = f"imported by '{rel_path}' — dependency needed for understanding"
                self._add_to_frontier(
                    target,
                    priority=self._score_frontier_item(target, distance=1),
                    reason=reason,
                    via=rel_path,
                    distance=1,
                )

        # Files that import this file (reverse deps — the "who uses this" direction)
        importers = self.brain._imported_by.get(rel_path, [])
        for imp in importers:
            if imp not in self.read_files:
                reason = f"imports '{rel_path}' — consumer that may be relevant to task"
                self._add_to_frontier(
                    imp,
                    priority=self._score_frontier_item(imp, distance=1) + 0.5,
                    reason=reason,
                    via=rel_path,
                    distance=1,
                )

    def _add_to_frontier(
        self,
        rel_path: str,
        priority: float,
        reason: str,
        via: str,
        distance: int,
    ) -> None:
        """Add a file to the frontier if not already there or already read."""
        if rel_path in self.read_files or rel_path in self._frontier_set:
            return
        item = FrontierItem(
            rel_path=rel_path,
            priority=priority,
            reason=reason,
            via=via,
            graph_distance=distance,
        )
        heapq.heappush(self._frontier_heap, item)
        self._frontier_set.add(rel_path)

    def _score_frontier_item(self, rel_path: str, distance: int) -> float:
        """Score a frontier item — lower = higher priority.

        Factors:
        - Task keyword relevance (from the brain's query scoring)
        - Graph distance from read files
        - Number of importers (hub score)
        - Behavioral relevance to the task
        """
        score = float(distance)  # Base: graph distance

        # Keyword relevance to the task
        from .brain import _extract_keywords, _path_multiplier
        keywords = _extract_keywords(self.task)
        blob = self.brain._blobs.get(rel_path, "")
        defns = self.brain._defn_sets.get(rel_path, set())

        keyword_hits = 0
        for kw in keywords:
            if kw in blob:
                keyword_hits += 2 if kw in defns else 1

        if keyword_hits > 0:
            score -= keyword_hits * 0.3  # More relevant = lower score = higher priority

        # Hub files (heavily imported) are usually important
        importer_count = self.brain._importer_counts.get(rel_path, 0)
        if importer_count > 0:
            score -= min(importer_count, 5) * 0.1

        # Path multiplier: penalize test/example dirs
        score += (1.0 - _path_multiplier(rel_path)) * 2.0

        return max(score, 0.01)  # Never go negative

    def _estimate_completeness(self) -> dict:
        """Estimate how much of the task-relevant context has been consumed."""
        total_relevant = len(self.read_files) + sum(
            1 for item in self._frontier_heap
            if item.rel_path not in self.read_files
        )
        if total_relevant == 0:
            pct = 100.0
        else:
            pct = round(len(self.read_files) / total_relevant * 100, 1)

        frontier_size = sum(
            1 for item in self._frontier_heap
            if item.rel_path not in self.read_files
        )

        if frontier_size == 0 and len(self.read_files) > 0:
            status = "complete"
            message = (
                "All reachable task-relevant files have been read. "
                "You have full context for this task."
            )
        elif frontier_size <= 2:
            status = "nearly_complete"
            message = (
                f"{frontier_size} file(s) remaining in the frontier. "
                "You're close to full understanding."
            )
        else:
            status = "in_progress"
            message = (
                f"{len(self.read_files)} files read, {frontier_size} remaining "
                f"in the frontier ({pct}% complete)."
            )

        return {
            "percent": pct,
            "status": status,
            "message": message,
            "files_read": len(self.read_files),
            "files_remaining": frontier_size,
        }


class SessionManager:
    """Manages multiple concurrent sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def start_session(self, brain: "ProjectBrain", task: str) -> Session:
        """Create a new session for a coding task.

        Runs the query engine to find initial entry points and seeds
        the frontier from them.
        """
        brain._ensure_fresh()

        session_id = uuid.uuid4().hex[:12]
        session = Session(
            session_id=session_id,
            task=task,
            brain=brain,
        )

        # Use the query engine to find initial files
        results = brain.query(task, top_n=5)
        initial_files = [r["rel_path"] for r in results]
        session._initial_files = initial_files

        # Seed the frontier with initial results
        for i, r in enumerate(results):
            rel_path = r["rel_path"]
            session._add_to_frontier(
                rel_path,
                priority=i * 0.1,  # Preserve query ranking
                reason=r.get("reason", "keyword match"),
                via="(initial query)",
                distance=0,
            )

        self._sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def list_sessions(self) -> list[dict]:
        return [
            {
                "session_id": s.session_id,
                "task": s.task,
                "read_count": len(s.read_files),
                "frontier_size": sum(
                    1 for item in s._frontier_heap
                    if item.rel_path not in s.read_files
                ),
                "created_at": s.created_at,
            }
            for s in self._sessions.values()
        ]

    def close_session(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None


def _normalize(path: str) -> str:
    """Normalize a file path to forward-slash relative form."""
    return str(PurePosixPath(path.replace("\\", "/")))
