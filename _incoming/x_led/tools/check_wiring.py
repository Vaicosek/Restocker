#!/usr/bin/env python3
"""check_wiring.py — both directions of the wiring contract, plus the two
smells that let round 3 ship safety machinery nothing ever called.

Round 3 added `outcome_known_for()`, `placements_needing_replay()`,
`reconcile_stake_placement()`, `PLACEMENT_IN_DOUBT_STATUSES` and friends to
estates_db.py, wrote docstrings claiming guarantees ("this is the ONE place that
judgement lives", "so a row can never be stranded"), and then never called any
of it from estates_main.py. The smoke test in use at the time checked that every
name CALLED exists. It could not possibly have caught this, because it only ever
looked in one direction.

This tool looks in both, and adds the two derived checks:

  1. FORWARD   every provider attribute a consumer names must resolve on the real
               imported module, every call site must bind against the real
               signature (arity + keywords), and every coroutine must be awaited
               or handed to a supervisor.
  2. REVERSE   every PUBLIC name a provider defines must have a caller somewhere
               in the codebase. Transitively: a helper whose only callers are
               themselves uncalled is uncalled. Each orphan is classified
               DEAD SAFETY MACHINERY (its own docstring claims a guarantee) or
               EXTENSION POINT (its own docstring says it is for callers who do
               not exist yet) or UNCLASSIFIED (a human must look).
  3. CONSTANTS every module-level constant a provider defines that NO OTHER
               module reads, same classification, plus whether its only internal
               readers are themselves dead (DEFINITE_REFUSAL_CODES was read only
               by outcome_known_for, which had no callers — dead by transitivity,
               two hops from anything running).
  4. DUPLICATE two functions in different modules that decide the SAME question.
               Heuristic and deliberately noisy: similar names, both predicates,
               overlapping string literals. `_outcome_known` vs
               `outcome_known_for` is the shape. A pair where one delegates to
               the other is reported as RESOLVED, not as a defect.

Exit status: 0 clean, 1 defects, 2 the tool could not run (import failure, bad
root). "Clean" is meant to be worth something: `--canary` proves the tool still
catches a planted dead function and a planted duplicate before you trust a
clean run.

Usage
-----
    python3 check_wiring.py                       # report over ./ (or --root)
    python3 check_wiring.py --root /home/claude/build
    python3 check_wiring.py --json                # machine-readable
    python3 check_wiring.py --canary              # self-test on a temp copy
    python3 check_wiring.py --strict              # duplicates also fail the run

Configuration is the CONFIG block below — providers, consumers, the words that
count as a guarantee claim. It is meant to be edited when the codebase grows a
third module, not forked.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import importlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# CONFIG
# --------------------------------------------------------------------------- #

#: Modules whose public surface is a contract other modules consume. Both
#: directions are checked for these: names they export must resolve, and names
#: they export must be called.
PROVIDERS = ["estates_db", "ledger_client"]

#: Classes whose *instances* are the API (methods reached as `client.hold(...)`,
#: never as `ledger_client.hold(...)`). Their public methods join the reverse
#: check as `LedgerClient.hold`.
API_CLASSES = {"ledger_client": ["LedgerClient"]}

#: Files that are not part of the codebase for usage purposes. This tool itself
#: must be here — it names every constant it checks, and would otherwise report
#: itself as the caller that keeps them alive.
#: `check_docstrings.py` and its fixtures join it for the same reason, and the
#: fixtures for a second one: they are deliberately-broken copies of real
#: functions, so every duplicate-judgement pair in the tree would be reported
#: twice more if they counted as code.
EXCLUDE_FILES = {"check_wiring.py", "check_docstrings.py",
                 "check_docstrings_fixtures.py", "check_docstrings_controls.py"}

#: Frozen-interface documents. Presence in one of these is recorded on a
#: finding: a published name with no caller is a documentation defect even when
#: it is not a safety defect.
INTERFACE_DOCS = {"estates_db": "ESTATES_DB_INTERFACE.md"}

#: Functions that legitimately receive an un-awaited coroutine (they own the
#: task lifecycle). A coroutine call passed to one of these is not a bug.
SUPERVISORS = {"spawn", "_supervise", "_dynamic_guard", "create_task",
               "ensure_future", "run", "run_until_complete", "gather",
               "run_on_bot_loop", "wait_for", "shield"}

#: A docstring containing one of these is claiming a guarantee. An uncalled
#: function that claims a guarantee is DEAD SAFETY MACHINERY: the claim is false
#: for the running program, which is worse than having no claim at all.
GUARANTEE_PATTERNS = [
    r"\bnever\b", r"\balways\b", r"\bcan(?:not|'t)\b", r"\bmust\b",
    r"\bguarantee", r"\bensures?\b", r"\bensuring\b", r"\binvariant",
    r"\bexactly once\b", r"\bat most once\b", r"\bthe ONE place\b",
    r"\bprovabl[ey]\b", r"\bthe only\b", r"\bso that\b", r"\bimpossible\b",
    r"\bdouble[- ](?:charge|charg|pay|paid|spend|spent|credit)",
    r"\bstranded\b", r"\bdeadlock", r"\bforever\b", r"\bsafe direction\b",
    r"\bwithout losing\b", r"\bloses? (?:coins|money)\b",
]

#: A docstring that tells a caller when to call it. If nothing calls it, the
#: instruction is a lie about the running program — `require_version`'s "Call
#: once at boot" with no boot caller means the version handshake never happens.
DIRECTIVE_PATTERNS = [
    r"\bcall (?:this|it|once|at boot|from|on every|before|after)\b",
    r"\bmust be called\b", r"\bcall(?:ed)? (?:at|on) (?:boot|start)",
    r"\brun (?:this|it) (?:at|on|every|before|after)\b",
    r"\bwire (?:this|it)\b",
]

#: Names shaped like recovery machinery. An uncalled function whose *name* is in
#: this family is dead safety machinery whether or not it bothered to say so.
SAFETY_NAME_PATTERNS = [
    r"reconcil", r"replay", r"sweep", r"recover", r"repair", r"unstick",
    r"unpark", r"unclaim", r"requeue", r"needing", r"unreconciled",
    r"unfinished", r"stuck", r"orphan", r"leak", r"in_doubt", r"refused",
    r"_check", r"verify", r"audit", r"guard", r"self_test",
]

#: A docstring saying "nobody calls this yet, on purpose". Checked BEFORE the
#: guarantee words, so a documented ops hook is not reported as a defect.
EXTENSION_PATTERNS = [
    r"\bno caller\b", r"\bnot called\b", r"\bnobody calls\b",
    r"\bextension point\b", r"\bfor callers who\b", r"\bwhen a caller\b",
    r"\bnot wired\b", r"\bops(?: console| only|-only)\b",
    r"\bdiagnostic\b", r"\bfor humans\b", r"\bby hand\b",
    r"\breserved for\b", r"\bfuture\b", r"\bconvenience\b",
    r"\bunused\b", r"\bREPL\b", r"\bfor staff to\b", r"\balias for\b",
]

#: Predicate shapes for the duplicate-judgement check.
PREDICATE_NAME_PATTERNS = [
    r"^_?(is|has|can|should|may|must|needs?|allow|permit)_",
    r"known", r"valid", r"allowed", r"eligible", r"refus", r"_ok$",
    r"safe", r"blocked", r"_for$", r"terminal", r"final",
]

#: Names every module is entitled to define for itself.
DUP_IGNORE_NAMES = {"main", "self_test", "usage"}

DUP_NAME_THRESHOLD = 0.62     # normalised-name similarity that alone flags a pair
DUP_LIT_THRESHOLD = 0.34       # string-literal Jaccard that flags a pair with a weak name match
DUP_WEAK_NAME = 0.30


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #

SEV_DEFECT = "DEFECT"
SEV_REVIEW = "REVIEW"
SEV_INFO = "INFO"


@dataclass
class Finding:
    check: str
    severity: str
    kind: str
    name: str
    location: str
    detail: str
    evidence: list[str] = field(default_factory=list)

    def line(self) -> str:
        return f"[{self.severity}] {self.check}/{self.kind}  {self.name}  ({self.location})"


# --------------------------------------------------------------------------- #
# Source model
# --------------------------------------------------------------------------- #

@dataclass
class Definition:
    name: str            # 'outcome_known_for' or 'LedgerClient.hold'
    kind: str            # function | async function | class | method | constant
    lineno: int
    doc: str
    module: str
    node: Any = None
    parent: str | None = None


class ModuleSource:
    """One .py file, parsed once, with the pieces every check needs."""

    def __init__(self, path: str):
        self.path = path
        self.modname = os.path.splitext(os.path.basename(path))[0]
        self.text = open(path, encoding="utf-8").read()
        self.lines = self.text.splitlines()
        self.tree = ast.parse(self.text, path)
        self.defs: dict[str, Definition] = {}
        self.top_nodes: dict[str, ast.AST] = {}
        self.module_level: list[ast.AST] = []
        self._collect()

    # -- comment-block docs ------------------------------------------------- #
    def comment_doc(self, lineno: int) -> str:
        """The `#:`/`#` block immediately above line `lineno` (1-indexed).

        estates_db documents its constants in `#:` blocks, not docstrings, so a
        constant check that only looked at docstrings would classify every
        constant as UNCLASSIFIED and be useless.
        """
        out: list[str] = []
        i = lineno - 2
        while i >= 0:
            s = self.lines[i].strip()
            if s.startswith("#"):
                out.append(s.lstrip("#:").lstrip("#").strip())
                i -= 1
                continue
            break
        doc = "\n".join(reversed(out))
        trail = self.inline_comment(lineno)
        return (doc + "\n" + trail).strip() if trail else doc

    def inline_comment(self, lineno: int) -> str:
        """The trailing `# …` on a definition line.

        `init_db = migrate  # bank_db.py calls it init_db()` documents itself
        there and nowhere else; a classifier that only reads the block above it
        calls that constant undocumented.
        """
        if not (1 <= lineno <= len(self.lines)):
            return ""
        raw = self.lines[lineno - 1]
        try:
            toks = list(tokenize.generate_tokens(iter([raw + "\n"]).__next__))
        except Exception:
            return ""
        return " ".join(t.string.lstrip("#").strip()
                        for t in toks if t.type == tokenize.COMMENT)

    def _collect(self) -> None:
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "async function" if isinstance(node, ast.AsyncFunctionDef) else "function"
                self.defs[node.name] = Definition(node.name, kind, node.lineno,
                                                  ast.get_docstring(node) or "",
                                                  self.modname, node)
                self.top_nodes[node.name] = node
            elif isinstance(node, ast.ClassDef):
                self.defs[node.name] = Definition(node.name, "class", node.lineno,
                                                  ast.get_docstring(node) or "",
                                                  self.modname, node)
                self.top_nodes[node.name] = node
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        q = f"{node.name}.{sub.name}"
                        k = "async method" if isinstance(sub, ast.AsyncFunctionDef) else "method"
                        self.defs[q] = Definition(q, k, sub.lineno,
                                                  ast.get_docstring(sub) or "",
                                                  self.modname, sub, parent=node.name)
                    elif isinstance(sub, ast.Assign):
                        # `get_balance = balance` inside the class body
                        for t in sub.targets:
                            if isinstance(t, ast.Name):
                                q = f"{node.name}.{t.id}"
                                self.defs[q] = Definition(
                                    q, "method alias", sub.lineno,
                                    self.comment_doc(sub.lineno), self.modname,
                                    sub, parent=node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                is_alias = isinstance(node.value, ast.Name)
                for t in targets:
                    if isinstance(t, ast.Name):
                        self.defs[t.id] = Definition(
                            t.id, "alias" if is_alias else "constant", node.lineno,
                            self.comment_doc(node.lineno), self.modname, node)
                        self.top_nodes.setdefault(t.id, node)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            else:
                self.module_level.append(node)

    # -- names referenced inside a subtree ---------------------------------- #
    @staticmethod
    def refs_in(node: ast.AST) -> set[str]:
        out: set[str] = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Name):
                out.add(n.id)
            elif isinstance(n, ast.Attribute):
                out.add(n.attr)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                # getattr(mod, "name") / dispatch tables keyed by name
                out.add(n.value)
        return out

    def string_literals(self, node: ast.AST) -> set[str]:
        return {n.value for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and 1 <= len(n.value) <= 60}


# --------------------------------------------------------------------------- #
# Alias resolution
# --------------------------------------------------------------------------- #

class Aliases:
    """Which local names in a consumer file mean which provider.

    Three bindings matter: `import estates_db as edb`, `from ledger_client
    import InsufficientFunds`, and `ledger = LedgerClient(...)` followed by
    `client = ledger` inside a dozen functions. The third is resolved to a fixed
    point, because that is how estates_main is actually written.
    """

    def __init__(self, src: ModuleSource, providers: list[str]):
        self.module_alias: dict[str, str] = {}      # edb -> estates_db
        self.from_import: dict[str, tuple[str, str]] = {}   # local -> (provider, orig)
        self.instance_alias: dict[str, tuple[str, str]] = {}  # ledger -> (ledger_client, LedgerClient)
        self.providers = providers
        self._resolve(src)

    def _resolve(self, src: ModuleSource) -> None:
        classes_by_name: dict[str, tuple[str, str]] = {}
        for prov, names in API_CLASSES.items():
            for c in names:
                classes_by_name[c] = (prov, c)

        for n in ast.walk(src.tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    if a.name in self.providers:
                        self.module_alias[a.asname or a.name] = a.name
            elif isinstance(n, ast.ImportFrom):
                if n.module in self.providers:
                    for a in n.names:
                        self.from_import[a.asname or a.name] = (n.module, a.name)

        # x = LedgerClient(...) — including a from-imported class name, and
        # including the shape estates_main actually uses:
        #     ledger: LedgerClient | None = (LedgerClient(...) if URL else None)
        # an AnnAssign whose value is an IfExp. Missing that shape silently
        # turned every client method into a false orphan, which is the exact
        # class of mistake this tool exists to stop, so it searches the whole
        # value subtree for the constructor call.
        seeds: dict[str, tuple[str, str]] = {}
        for n in ast.walk(src.tree):
            if not isinstance(n, (ast.Assign, ast.AnnAssign)) or n.value is None:
                continue
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            cname = None
            for sub in ast.walk(n.value):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    c = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
                    if c in classes_by_name:
                        cname = c
                        break
            if cname:
                for t in targets:
                    if isinstance(t, ast.Name):
                        seeds[t.id] = classes_by_name[cname]
        self.instance_alias.update(seeds)

        # propagate `client = ledger` to a fixed point
        for _ in range(8):
            changed = False
            for n in ast.walk(src.tree):
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Name):
                    origin = self.instance_alias.get(n.value.id)
                    if origin:
                        for t in n.targets:
                            if isinstance(t, ast.Name) and t.id not in self.instance_alias:
                                self.instance_alias[t.id] = origin
                                changed = True
            if not changed:
                break

    def resolve_attr(self, base: str, attr: str) -> tuple[str, str] | None:
        """(provider, qualified_name) for `base.attr`, or None."""
        if base in self.module_alias:
            return self.module_alias[base], attr
        if base in self.instance_alias:
            prov, cls = self.instance_alias[base]
            return prov, f"{cls}.{attr}"
        return None


# --------------------------------------------------------------------------- #
# Usage index
# --------------------------------------------------------------------------- #

class Usage:
    def __init__(self) -> None:
        self.qualified: dict[tuple[str, str], list[str]] = {}   # (prov,name) -> ["file:line"]
        self.loose_attr: dict[str, list[str]] = {}
        self.bare: dict[str, list[str]] = {}
        self.strings: dict[str, list[str]] = {}

    @staticmethod
    def _add(d: dict, k, v) -> None:
        d.setdefault(k, []).append(v)

    def scan(self, src: ModuleSource, al: Aliases) -> None:
        f = os.path.basename(src.path)
        for n in ast.walk(src.tree):
            if isinstance(n, ast.Attribute):
                loc = f"{f}:{n.lineno}"
                if isinstance(n.value, ast.Name):
                    hit = al.resolve_attr(n.value.id, n.attr)
                    if hit:
                        self._add(self.qualified, hit, loc)
                        continue
                self._add(self.loose_attr, n.attr, loc)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                loc = f"{f}:{n.lineno}"
                hit = al.from_import.get(n.id)
                if hit:
                    self._add(self.qualified, hit, loc)
                else:
                    self._add(self.bare, n.id, loc)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                if 1 <= len(n.value) <= 80:
                    self._add(self.strings, n.value, f"{f}:{n.lineno}")


# --------------------------------------------------------------------------- #
# 1. FORWARD check
# --------------------------------------------------------------------------- #

_SENTINEL = object()


def forward_check(consumers: list[ModuleSource], aliases: dict[str, Aliases],
                  mods: dict[str, Any]) -> tuple[list[Finding], dict]:
    findings: list[Finding] = []
    stats = {"names": 0, "missing": 0, "bound": 0, "arity_failures": 0,
             "skipped_binds": 0, "unawaited": 0, "from_imports": 0}
    seen_names: set[tuple[str, str]] = set()

    for src in consumers:
        al = aliases[src.modname]
        fname = os.path.basename(src.path)

        # --- (a) every named attribute resolves -----------------------------
        for n in ast.walk(src.tree):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
                hit = al.resolve_attr(n.value.id, n.attr)
                if not hit:
                    continue
                prov, qname = hit
                seen_names.add(hit)
                if _lookup(mods, prov, qname) is _SENTINEL:
                    findings.append(Finding(
                        "forward", SEV_DEFECT, "missing-name", f"{prov}.{qname}",
                        f"{fname}:{n.lineno}",
                        f"{fname} names {n.value.id}.{n.attr}, which does not exist on the "
                        f"imported {prov}."))
            elif isinstance(n, ast.ImportFrom) and n.module in mods:
                for a in n.names:
                    stats["from_imports"] += 1
                    if not hasattr(mods[n.module], a.name):
                        findings.append(Finding(
                            "forward", SEV_DEFECT, "missing-import",
                            f"{n.module}.{a.name}", f"{fname}:{n.lineno}",
                            f"`from {n.module} import {a.name}` does not resolve."))

        # --- (b) every call site binds against the real signature -----------
        for n in ast.walk(src.tree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)):
                continue
            hit = al.resolve_attr(f.value.id, f.attr)
            if not hit:
                continue
            prov, qname = hit
            target = _lookup(mods, prov, qname)
            if target is _SENTINEL or not callable(target):
                continue
            try:
                sig = inspect.signature(target)
            except (TypeError, ValueError):
                stats["skipped_binds"] += 1
                continue
            if any(isinstance(a, ast.Starred) for a in n.args) or \
               any(k.arg is None for k in n.keywords):
                stats["skipped_binds"] += 1
                continue
            # an unbound method reached through an instance still needs `self`
            nargs = len(n.args) + (1 if "." in qname else 0)
            kw = {k.arg: _SENTINEL for k in n.keywords}
            try:
                sig.bind(*([_SENTINEL] * nargs), **kw)
                stats["bound"] += 1
            except TypeError as e:
                stats["arity_failures"] += 1
                findings.append(Finding(
                    "forward", SEV_DEFECT, "signature", f"{prov}.{qname}",
                    f"{fname}:{n.lineno}",
                    f"call does not bind against the real signature "
                    f"{qname}{sig}: {e}"))

        # --- (c) every coroutine is awaited or supervised -------------------
        awaited = {id(n.value) for n in ast.walk(src.tree)
                   if isinstance(n, ast.Await) and isinstance(n.value, ast.Call)}
        supervised: set[int] = set()
        for n in ast.walk(src.tree):
            if isinstance(n, ast.Call):
                fn = n.func
                nm = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
                if nm in SUPERVISORS:
                    for a in list(n.args) + [k.value for k in n.keywords]:
                        if isinstance(a, ast.Call):
                            supervised.add(id(a))
        for n in ast.walk(src.tree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)):
                continue
            hit = al.resolve_attr(f.value.id, f.attr)
            if not hit:
                continue
            target = _lookup(mods, *hit)
            if target is _SENTINEL or not inspect.iscoroutinefunction(target):
                continue
            if id(n) not in awaited and id(n) not in supervised:
                stats["unawaited"] += 1
                findings.append(Finding(
                    "forward", SEV_DEFECT, "unawaited", f"{hit[0]}.{hit[1]}",
                    f"{fname}:{n.lineno}",
                    "coroutine call is neither awaited nor handed to a supervisor; "
                    "it never runs and the failure is silent."))

    stats["names"] = len(seen_names)
    stats["missing"] = sum(1 for f in findings if f.kind == "missing-name")
    return findings, stats


def _lookup(mods: dict[str, Any], prov: str, qname: str) -> Any:
    mod = mods.get(prov)
    if mod is None:
        return _SENTINEL
    obj: Any = mod
    for part in qname.split("."):
        obj = getattr(obj, part, _SENTINEL)
        if obj is _SENTINEL:
            return _SENTINEL
    return obj


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

def _matches(doc: str, patterns: Iterable[str]) -> list[str]:
    return [p for p in patterns if re.search(p, doc, re.IGNORECASE)]


def is_thin_binding(src: "ModuleSource", d: Definition) -> bool:
    """True for a method that is only a wrapper over a remote endpoint.

    `LedgerClient.stock_buy` is `self._require_key(...)` then `self._request(...)`:
    every call it makes is to a private helper. An uncalled wrapper like that is
    an unused *binding* — this codebase does not use that endpoint — which is a
    different and much smaller thing than a guard nothing runs. Keeping the two
    apart is what stops the DEAD_SAFETY list filling with client methods and
    losing its meaning.
    """
    if not isinstance(d.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    calls = [n for n in ast.walk(d.node) if isinstance(n, ast.Call)]
    if not calls:
        return False
    # Only a call into this module's own public surface counts as logic.
    # `str(user_id)`, `(reason or "").strip()` and `_coins(...)` do not.
    # Classes are excluded: `raise BadRequest(...)` is argument validation, not
    # domain logic, and a wrapper that validates its arguments before posting is
    # still a wrapper.
    public_here = {n for n, dd in src.defs.items()
                   if not n.startswith("_")
                   and dd.kind not in ("constant", "class", "alias")}
    own_methods = {n.split(".")[1] for n in public_here if "." in n}
    for c in calls:
        f = c.func
        if isinstance(f, ast.Attribute):
            if isinstance(f.value, ast.Name) and f.value.id in ("self", "cls"):
                if not f.attr.startswith("_") and f.attr in own_methods:
                    return False
        elif isinstance(f, ast.Name):
            if f.id in public_here:
                return False
    return True


def classify(name: str, doc: str) -> tuple[str, str]:
    """(classification, why) for a public name with no caller."""
    ext = _matches(doc, EXTENSION_PATTERNS)
    if ext:
        return ("EXTENSION_POINT",
                f"docstring documents it as uncalled on purpose ({', '.join(ext[:3])})")
    dir_ = _matches(doc, DIRECTIVE_PATTERNS)
    if dir_:
        return ("DEAD_SAFETY_MACHINERY",
                f"the docstring instructs a caller when to call it "
                f"({', '.join(dir_[:2])}) and no caller exists — the instruction "
                f"describes a program that is not this one")
    gua = _matches(doc, GUARANTEE_PATTERNS)
    nm = _matches(name.split(".")[-1], SAFETY_NAME_PATTERNS)
    if gua and nm:
        return ("DEAD_SAFETY_MACHINERY",
                f"safety-shaped name and a docstring claiming a guarantee "
                f"({', '.join(gua[:3])})")
    if gua:
        return ("DEAD_SAFETY_MACHINERY",
                f"docstring claims a guarantee the running code does not have "
                f"({', '.join(gua[:3])})")
    if nm:
        return ("DEAD_SAFETY_MACHINERY",
                f"name is recovery/verification machinery ({', '.join(nm[:3])}) "
                f"with no invoker")
    if not doc.strip():
        return ("UNCLASSIFIED", "no docstring and no caller — nothing states its purpose")
    return ("UNCLASSIFIED", "no guarantee language; a human must decide whether this "
                            "is an extension point or an oversight")


# --------------------------------------------------------------------------- #
# 2. REVERSE check
# --------------------------------------------------------------------------- #

def reachable_within(src: ModuleSource, roots: set[str],
                     skip: frozenset[str] | set[str] = frozenset()) -> set[str]:
    """Top-level names reachable from `roots` by intra-module reference.

    Transitivity is the point: DEFINITE_REFUSAL_CODES had one internal reader,
    outcome_known_for, which had no callers at all. A one-hop check calls the
    constant "used"; this one calls it dead.
    """
    graph = build_graph(src)
    seen: set[str] = set()
    stack = [r for r in roots if r in graph and r not in skip]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend({r for r in graph.get(cur, set())
                      if r in graph and r not in skip and r not in seen})
    return seen


#: Methods that run because the class is used at all, not because anything names
#: them. Constructing `LedgerClient(...)` runs `__init__`; `async with` runs the
#: context-manager pair.
DUNDER_ENTRY = {"__init__", "__new__", "__enter__", "__exit__",
                "__aenter__", "__aexit__", "__del__", "__repr__", "__str__"}


def _edges(node: ast.AST, cls: str | None = None) -> set[str]:
    """Names a definition depends on. `self.x` inside class C becomes `C.x`.

    Keeping the `self.` qualifier is what makes the graph method-granular: a
    constant read only by `LedgerClient.check_version` must not look live just
    because some *other* method of the same class is called every 120 s.
    """
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            if cls and isinstance(n.value, ast.Name) and n.value.id in ("self", "cls"):
                out.add(f"{cls}.{n.attr}")
            else:
                out.add(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.add(n.value)          # getattr / dispatch-table dependency
    return out


def build_graph(src: ModuleSource) -> dict[str, set[str]]:
    """`name -> names it references`, with class methods as their own nodes."""
    g: dict[str, set[str]] = {}
    for name, node in src.top_nodes.items():
        if isinstance(node, ast.ClassDef):
            cls_refs: set[str] = set()
            for b in list(node.bases) + list(node.decorator_list):
                cls_refs |= _edges(b)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    g[f"{name}.{sub.name}"] = _edges(sub, cls=name) - {f"{name}.{sub.name}"}
                elif isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Name):
                    for t in sub.targets:   # get_balance = balance
                        if isinstance(t, ast.Name):
                            g[f"{name}.{t.id}"] = {f"{name}.{sub.value.id}"}
                else:
                    cls_refs |= _edges(sub, cls=name)
            cls_refs |= {f"{name}.{m}" for m in DUNDER_ENTRY}
            g[name] = cls_refs - {name}
        else:
            g[name] = _edges(node) - {name}
    return g


#: Functions that only the module's own self-test runs. Reaching a name from
#: one of these is NOT the same as the program using it: estates_db's
#: `_n6_refusal_regression` asserts `outcome_known_for(...)` in both directions,
#: which is why a naive reachability check calls it live while every production
#: path still ignores it. That is round 3's defect surviving the check that was
#: supposed to catch it.
TEST_NAME_PATTERNS = [r"^_?self_?test", r"^_?test_", r"_regression$", r"^_self_check",
                      r"_self_test", r"^_?check_self"]


def _is_test_name(n: str) -> bool:
    return any(re.search(p, n) for p in TEST_NAME_PATTERNS)


def _is_main_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    t = node.test
    return (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
            and t.left.id == "__name__")


def module_level_roots(src: ModuleSource, include_tests: bool = True) -> set[str]:
    """Top-level names referenced by module-level code.

    `include_tests=False` drops the `if __name__ == "__main__":` block and every
    test-shaped function, giving the set of names the *program* uses as opposed
    to the set the module's own assertions touch.
    """
    roots: set[str] = set()
    for node in src.module_level:
        if not include_tests and _is_main_guard(node):
            continue
        roots |= ModuleSource.refs_in(node)
    out = {r for r in roots if r in src.top_nodes}
    if not include_tests:
        out = {r for r in out if not _is_test_name(r)}
    return out


def reverse_check(provider: ModuleSource, usage: Usage, iface_names: set[str],
                  siblings: dict[str, list[str]] | None = None,
                  ) -> tuple[list[Finding], dict]:
    prov = provider.modname
    findings: list[Finding] = []
    publics = {n: d for n, d in provider.defs.items()
               if not any(p.startswith("_") for p in n.split("."))
               and (d.kind != "constant" or True)}

    # Which public names does anything outside this module use?
    external: dict[str, list[str]] = {}
    loose: dict[str, list[str]] = {}
    strung: dict[str, list[str]] = {}
    for name in publics:
        hits = [h for h in usage.qualified.get((prov, name), [])
                if not h.startswith(os.path.basename(provider.path) + ":")]
        # a method may also be reached through an alias this tool could not
        # resolve (self.client.hold, a dict of clients) — recorded, not trusted
        short = name.split(".")[-1]
        lo = [h for h in usage.loose_attr.get(short, [])
              if not h.startswith(os.path.basename(provider.path) + ":")]
        st = [h for h in usage.strings.get(short, [])
              if not h.startswith(os.path.basename(provider.path) + ":")]
        if hits:
            external[name] = hits
        if lo:
            loose[name] = lo
        if st:
            strung[name] = st

    # One graph, method-granular. Roots are what the rest of the codebase calls
    # (`edb.migrate`, `LedgerClient.hold`) plus this module's own module-level
    # code; `live_prod` additionally drops the self-test roots.
    ext_roots = set(external) | {name.split(".")[0] for name in external}
    live = reachable_within(
        provider, set(module_level_roots(provider, include_tests=True)) | ext_roots)
    tests = {n for n in provider.top_nodes if _is_test_name(n)}
    live_prod = reachable_within(
        provider, set(module_level_roots(provider, include_tests=False)) | ext_roots,
        skip=tests)
    exported = dunder_all(provider)

    orphans: list[tuple[str, Definition, str]] = []
    for name, d in sorted(publics.items(), key=lambda kv: kv[1].lineno):
        if d.kind == "constant":
            continue                      # handled by constant_check
        if name in external:
            continue
        if d.parent and d.parent not in API_CLASSES.get(prov, []):
            continue                      # methods of non-API classes are internal
        if name in live_prod:
            continue
        if name in live:
            reason = ("TEST-ONLY: the only paths that reach it are this "
                      "module's own self-test / regression functions. Nothing "
                      "the bot runs calls it")
        elif d.parent:
            reason = ("no call site anywhere resolves to this method, and no method "
                      "that is itself called reaches it through `self`")
        else:
            reason = ("no caller anywhere, and nothing that is itself reachable "
                      "references it")
        orphans.append((name, d, reason))

    for name, d, reason in orphans:
        cls_, why = classify(name, d.doc)
        if reason.startswith("TEST-ONLY") and cls_ != "EXTENSION_POINT":
            why = ("exercised only by the module's own assertions, so the "
                   "docstring's promise holds in the self-test and nowhere else. "
                   + why)
            if cls_ == "UNCLASSIFIED":
                cls_ = "TEST_ONLY_NO_PRODUCTION_CALLER"
        if cls_ != "EXTENSION_POINT" and d.parent and is_thin_binding(provider, d):
            cls_ = "UNUSED_API_BINDING"
            why = ("a transport wrapper whose every call is to a private helper — "
                   "this codebase never uses that endpoint. Not a broken guarantee, "
                   "but its docstring is unexercised prose")
        sev = SEV_DEFECT if cls_ == "DEAD_SAFETY_MACHINERY" else SEV_REVIEW
        ev = [reason]
        if name in loose:
            ev.append(f"POSSIBLE unresolved use (attribute of the same name, base "
                      f"not resolvable): {', '.join(loose[name][:4])} — verify by hand")
            if sev == SEV_DEFECT:
                sev = SEV_REVIEW
        if name in strung:
            ev.append(f"name appears as a string literal at {', '.join(strung[name][:3])} "
                      f"— possible getattr dispatch")
        if name in iface_names:
            ev.append("published in the frozen interface document, so the contract "
                      "advertises a name nothing calls")
        if name.split(".")[-1] in exported and "." not in name:
            ev.append("listed in this module's __all__")
        sib = [x for x in (siblings or {}).get(_norm_name(name), [])
               if not x.startswith(prov + ".")]
        if sib:
            ev.append(f"a definition of the same name is live elsewhere: "
                      f"{', '.join(sib)} — this copy is the one nothing calls, and "
                      f"nothing makes the two agree")
        doc1 = (d.doc.strip().splitlines() or [""])[0]
        findings.append(Finding(
            "reverse", sev, cls_, f"{prov}.{name}",
            f"{os.path.basename(provider.path)}:{d.lineno}",
            f"{d.kind} with no caller: {why}."
            + (f' Docstring opens: "{doc1[:110]}"' if doc1 else ""),
            ev))

    stats = {"public_names": len([n for n, d in publics.items() if d.kind != "constant"]),
             "with_callers": len([n for n in external if publics[n].kind != "constant"]),
             "orphans": len(orphans),
             "test_only": sum(1 for _, _, r in orphans if r.startswith("TEST-ONLY"))}
    # names no production path reaches — what the constant check needs to know
    # before it calls an internal reader "live"
    nonprod = {n for n in build_graph(provider) if n not in live_prod}
    return findings, stats, nonprod


def dunder_all(src: ModuleSource) -> set[str]:
    node = src.top_nodes.get("__all__")
    if node is None:
        return set()
    return {n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


# --------------------------------------------------------------------------- #
# 3. CONSTANT check
# --------------------------------------------------------------------------- #

def _readers_of(provider: ModuleSource, name: str) -> list[str]:
    """Which definitions read `name`, at method granularity inside classes.

    Class granularity would be a hole: `EXPECTED_API_VERSION` is read only by
    `LedgerClient.check_version`, and calling the whole class its reader would
    report a constant as live when the one method that reads it never runs.
    """
    out: list[str] = []
    for rd, node in provider.top_nodes.items():
        if rd == name or rd == "__all__":
            continue     # exporting a name is not reading it
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if name in ModuleSource.refs_in(sub):
                        out.append(f"{rd}.{sub.name}")
                elif name in ModuleSource.refs_in(sub):
                    out.append(rd)
        elif name in ModuleSource.refs_in(node):
            out.append(rd)
    return sorted(set(out))


def constant_check(provider: ModuleSource, usage: Usage,
                   dead_names: set[str]) -> tuple[list[Finding], dict]:
    prov = provider.modname
    own = os.path.basename(provider.path)
    findings: list[Finding] = []
    consts = {n: d for n, d in provider.defs.items()
              if d.kind == "constant" and not n.startswith("_")}

    checked = 0
    for name, d in sorted(consts.items(), key=lambda kv: kv[1].lineno):
        checked += 1
        ext = [h for h in usage.qualified.get((prov, name), []) if not h.startswith(own + ":")]
        if ext:
            continue
        loose = [h for h in usage.loose_attr.get(name, []) if not h.startswith(own + ":")]
        internal = _readers_of(provider, name)
        internal_live = [r for r in internal if r not in dead_names]
        cls_, why = classify(name, d.doc)

        if internal_live:
            sev, kind = SEV_INFO, "INTERNAL_ONLY"
            detail = (f"no other module reads it; internal readers on a live path: "
                      f"{', '.join(internal_live[:6])}")
        elif internal:
            sev = SEV_DEFECT if cls_ != "EXTENSION_POINT" else SEV_REVIEW
            kind = "DEAD_BY_TRANSITIVITY" if cls_ != "EXTENSION_POINT" else cls_
            detail = (f"no other module reads it, and its only internal readers "
                      f"({', '.join(internal[:6])}) are themselves unreachable from "
                      f"any production path — dead or self-test-only, two hops from "
                      f"anything that runs. {why}")
        else:
            sev = SEV_DEFECT if cls_ == "DEAD_SAFETY_MACHINERY" else SEV_REVIEW
            kind = cls_
            detail = f"defined and read by nothing, in this module or any other. {why}"

        ev = []
        if loose:
            ev.append(f"POSSIBLE unresolved read: {', '.join(loose[:4])}")
            sev = SEV_REVIEW if sev == SEV_DEFECT else sev
        doc1 = (d.doc.strip().splitlines() or [""])[0]
        if doc1:
            ev.append(f'documented: "{doc1[:120]}"')
        findings.append(Finding("constant", sev, kind, f"{prov}.{name}",
                                f"{own}:{d.lineno}", detail, ev))

    return findings, {"constants": checked,
                      "unread_externally": len(findings)}


# --------------------------------------------------------------------------- #
# 4. DUPLICATE-JUDGEMENT check
# --------------------------------------------------------------------------- #

def _is_predicate(src: ModuleSource, d: Definition) -> bool:
    node = d.node
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    ann = getattr(node, "returns", None)
    if isinstance(ann, ast.Name) and ann.id == "bool":
        return True
    if any(re.search(p, d.name.split(".")[-1], re.IGNORECASE)
           for p in PREDICATE_NAME_PATTERNS):
        rets = [n for n in ast.walk(node) if isinstance(n, ast.Return) and n.value is not None]
        if not rets:
            return False
        boolish = 0
        for r in rets:
            v = r.value
            if (isinstance(v, ast.Constant) and isinstance(v.value, bool)) or \
               isinstance(v, (ast.Compare, ast.BoolOp, ast.UnaryOp)) or \
               (isinstance(v, ast.Call) and getattr(v.func, "attr", "") in
                ("startswith", "endswith", "issubset")) or \
               (isinstance(v, ast.Call) and getattr(v.func, "id", "") == "bool"):
                boolish += 1
            elif isinstance(v, ast.Call):
                boolish += 1          # delegation returns whatever the callee does
        return boolish == len(rets)
    return False


def _norm_name(n: str) -> str:
    n = n.split(".")[-1].strip("_").lower()
    n = re.sub(r"_(for|of|from|on|in|by|to)$", "", n)
    return n


def _tokens(n: str) -> set[str]:
    return {t for t in re.split(r"[_\W]+", _norm_name(n)) if t}


def _delegates_to(a_src: ModuleSource, a: Definition, b: Definition) -> bool:
    short = b.name.split(".")[-1]
    for n in ast.walk(a.node):
        if isinstance(n, ast.Call):
            f = n.func
            if getattr(f, "attr", None) == short or getattr(f, "id", None) == short:
                return True
    return False


def duplicate_check(sources: list[ModuleSource]) -> tuple[list[Finding], dict]:
    preds: list[tuple[ModuleSource, Definition, set[str]]] = []
    for s in sources:
        for d in s.defs.values():
            if _is_predicate(s, d):
                preds.append((s, d, s.string_literals(d.node)))

    findings: list[Finding] = []

    # (a) same public name defined in two modules. Not a predicate rule — it is
    #     the other way two modules end up deciding one question. estates_db.
    #     mint_key and ledger_client.mint_key both build the idempotency key
    #     that decides whether a retry is a replay or a second charge.
    by_short: dict[str, list[tuple[ModuleSource, Definition]]] = {}
    for s in sources:
        for d in s.defs.values():
            if d.kind in ("function", "async function") and not d.name.startswith("_"):
                by_short.setdefault(_norm_name(d.name), []).append((s, d))
    for short, group in sorted(by_short.items()):
        mods_ = {s.modname for s, _ in group}
        if len(mods_) < 2 or short in DUP_IGNORE_NAMES:
            continue
        locs = ", ".join(f"{os.path.basename(s.path)}:{d.lineno}" for s, d in group)
        findings.append(Finding(
            "duplicate", SEV_REVIEW, "DUPLICATE_DEFINITION", short, locs,
            f"`{short}` is defined in {len(mods_)} modules ({', '.join(sorted(mods_))}). "
            f"Two implementations of one rule drift silently — if they are meant to "
            f"agree, one must call the other or the agreement is an assumption.",
            [f'{s.modname}.{d.name}: "{(d.doc.splitlines() or [""])[0][:90]}"'
             for s, d in group]))

    compared = 0
    for i in range(len(preds)):
        for j in range(i + 1, len(preds)):
            sa, a, la = preds[i]
            sb, b, lb = preds[j]
            if sa.modname == sb.modname:
                continue
            compared += 1
            na, nb = _norm_name(a.name), _norm_name(b.name)
            seq = difflib.SequenceMatcher(None, na, nb).ratio()
            ta, tb = _tokens(a.name), _tokens(b.name)
            jac_n = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
            name_score = max(seq, jac_n)
            lit = (len(la & lb) / len(la | lb)) if (la and lb) else 0.0
            if not (name_score >= DUP_NAME_THRESHOLD or
                    (lit >= DUP_LIT_THRESHOLD and name_score >= DUP_WEAK_NAME)):
                continue
            deleg = _delegates_to(sa, a, b) or _delegates_to(sb, b, a)
            shared = sorted(la & lb)[:6]
            detail = (f"{sa.modname}.{a.name} and {sb.modname}.{b.name} are both "
                      f"predicates with similar names (score {name_score:.2f}"
                      + (f", shared string literals {lit:.2f}: {shared}" if lit else "")
                      + "). Two modules deciding the same question drift; one of them "
                        "should ask the other.")
            if deleg:
                findings.append(Finding(
                    "duplicate", SEV_INFO, "RESOLVED_DELEGATION",
                    f"{a.name} / {b.name}",
                    f"{os.path.basename(sa.path)}:{a.lineno} + "
                    f"{os.path.basename(sb.path)}:{b.lineno}",
                    detail + " One already calls the other, so there is a single "
                             "source of truth — no action.", []))
            else:
                findings.append(Finding(
                    "duplicate", SEV_REVIEW, "DUPLICATE_JUDGEMENT",
                    f"{a.name} / {b.name}",
                    f"{os.path.basename(sa.path)}:{a.lineno} + "
                    f"{os.path.basename(sb.path)}:{b.lineno}",
                    detail + " Neither calls the other: verify by hand that they "
                             "cannot disagree.",
                    [f'{a.name} doc: "{(a.doc.splitlines() or [""])[0][:90]}"',
                     f'{b.name} doc: "{(b.doc.splitlines() or [""])[0][:90]}"']))
    return findings, {"predicates": len(preds), "pairs_compared": compared}


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

def interface_names(root: str, provider: str) -> set[str]:
    """Names the frozen contract for `provider` publishes.

    Keyed by provider: `mint_key` is in estates_db's interface document, and
    attributing that to `ledger_client.mint_key` — a different function, with
    different arguments — would put a false line on a real finding.
    """
    out: set[str] = set()
    doc = INTERFACE_DOCS.get(provider)
    if doc:
        p = os.path.join(root, doc)
        if not os.path.exists(p):
            return out
        for line in open(p, encoding="utf-8"):
            line = line.rstrip("\n")
            if line.startswith("def "):
                out.add(line[4:].split("(")[0].strip())
            elif line.startswith("class "):
                out.add(line[6:].split("(")[0].split(":")[0].strip())
            elif line and not line.startswith((" ", "#", "|", "-")) and "=" in line:
                out.add(line.split("=")[0].split(":")[0].strip())
    return out


def import_providers(root: str, providers: list[str]) -> dict[str, Any]:
    sys.path.insert(0, root)
    os.environ.setdefault("ESTATES_DB_PATH",
                          os.path.join(tempfile.gettempdir(), "check_wiring_probe.db"))
    mods: dict[str, Any] = {}
    for p in providers:
        for stale in [m for m in sys.modules if m == p]:
            del sys.modules[stale]
        mods[p] = importlib.import_module(p)
    return mods


def run(root: str, strict: bool = False) -> tuple[list[Finding], dict]:
    root = os.path.abspath(root)
    files = sorted(f for f in os.listdir(root)
                   if f.endswith(".py") and f not in EXCLUDE_FILES)
    if not files:
        raise SystemExit(f"check_wiring: no .py files under {root}")

    sources = [ModuleSource(os.path.join(root, f)) for f in files]
    by_name = {s.modname: s for s in sources}
    present_providers = [p for p in PROVIDERS if p in by_name]
    aliases = {s.modname: Aliases(s, present_providers) for s in sources}

    usage = Usage()
    for s in sources:
        usage.scan(s, aliases[s.modname])

    mods = import_providers(root, present_providers)
    consumers = [s for s in sources if s.modname not in present_providers]
    # a provider may consume another provider
    consumers += [s for s in sources if s.modname in present_providers
                  and any(aliases[s.modname].module_alias.get(a) in present_providers
                          for a in aliases[s.modname].module_alias)]

    findings: list[Finding] = []
    stats: dict[str, Any] = {}

    f, st = forward_check(consumers, aliases, mods)
    findings += f
    stats["forward"] = st

    siblings: dict[str, list[str]] = {}
    for s in sources:
        for d in s.defs.values():
            if d.kind in ("function", "async function") and not d.name.startswith("_"):
                siblings.setdefault(_norm_name(d.name), []).append(f"{s.modname}.{d.name}")
    dead_by_provider: dict[str, set[str]] = {}
    for p in present_providers:
        f, st, nonprod = reverse_check(by_name[p], usage,
                                      interface_names(root, p), siblings)
        findings += f
        stats.setdefault("reverse", {})[p] = st
        dead_by_provider[p] = nonprod | {x.name.split(".", 1)[1] for x in f
                                         if x.check == "reverse"}

    for p in present_providers:
        f, st = constant_check(by_name[p], usage, dead_by_provider.get(p, set()))
        findings += f
        stats.setdefault("constants", {})[p] = st

    f, st = duplicate_check(sources)
    findings += f
    stats["duplicate"] = st

    if strict:
        for x in findings:
            if x.check == "duplicate" and x.kind == "DUPLICATE_JUDGEMENT":
                x.severity = SEV_DEFECT
    stats["files"] = files
    return findings, stats


_ORDER = {SEV_DEFECT: 0, SEV_REVIEW: 1, SEV_INFO: 2}


def report(findings: list[Finding], stats: dict, quiet: bool = False) -> None:
    w = sys.stdout.write
    w("=" * 78 + "\ncheck_wiring — forward, reverse, constants, duplicate judgement\n")
    w("=" * 78 + "\n")
    fs = stats.get("forward", {})
    w(f"\nfiles scanned: {', '.join(stats.get('files', []))}\n")
    w(f"\n[1] FORWARD  provider names referenced: {fs.get('names', 0)}  "
      f"missing: {fs.get('missing', 0)}  call sites bound: {fs.get('bound', 0)}  "
      f"arity failures: {fs.get('arity_failures', 0)}  un-awaited coroutines: "
      f"{fs.get('unawaited', 0)}  (skipped binds: {fs.get('skipped_binds', 0)})\n")
    for p, st in stats.get("reverse", {}).items():
        w(f"[2] REVERSE  {p}: {st['public_names']} public callables, "
          f"{st['with_callers']} with callers, {st['orphans']} with none\n")
    for p, st in stats.get("constants", {}).items():
        w(f"[3] CONSTS   {p}: {st['constants']} public constants, "
          f"{st['unread_externally']} read by no other module\n")
    ds = stats.get("duplicate", {})
    w(f"[4] DUPLICATE {ds.get('predicates', 0)} predicate-shaped functions, "
      f"{ds.get('pairs_compared', 0)} cross-module pairs compared\n\n")

    for sev in (SEV_DEFECT, SEV_REVIEW, SEV_INFO):
        group = [f for f in findings if f.severity == sev]
        if not group or (quiet and sev == SEV_INFO):
            continue
        w("-" * 78 + f"\n{sev}  ({len(group)})\n" + "-" * 78 + "\n")
        for f in group:
            w(f"\n{f.line()}\n    {f.detail}\n")
            for e in f.evidence:
                w(f"    - {e}\n")
    n_def = sum(1 for f in findings if f.severity == SEV_DEFECT)
    w("\n" + "=" * 78 + f"\nRESULT: {n_def} defect(s), "
      f"{sum(1 for f in findings if f.severity == SEV_REVIEW)} for review\n")


# --------------------------------------------------------------------------- #
# Canary — a clean run only means something if the tool can still fail
# --------------------------------------------------------------------------- #

CANARY_DEAD = '''

def canary_reconcile_orphans(limit: int = 50) -> list[int]:
    """Return rows that must never be left stranded.

    This is the ONE place that judgement lives; a caller can never lose coins
    while this guard runs.
    """
    return [int(limit)]
'''

CANARY_DUP_A = '''

def canary_outcome_is_final(code: str) -> bool:
    """True when core provably refused."""
    return code in ("insufficient", "frozen", "escrow_shortfall", "bad_request")
'''

CANARY_DUP_B = '''

def _canary_outcome_final(code: str) -> bool:
    """True when core provably refused (second opinion)."""
    return code in ("insufficient", "frozen", "escrow_shortfall", "bad_request")
'''

#: The forward direction has to keep biting too — a name that does not exist, a
#: call that cannot bind, and a coroutine nobody awaits.
CANARY_FORWARD = '''

async def _canary_forward_probe(uid: str) -> None:
    edb.canary_no_such_name(1)
    edb.mint_key()
    ledger.balance(uid)
'''


def canary(root: str) -> int:
    """Plant a dead function and a duplicate judgement in a copy; require both."""
    root = os.path.abspath(root)
    tmp = tempfile.mkdtemp(prefix="check_wiring_canary_")
    dest = os.path.join(tmp, "build")
    shutil.copytree(root, dest, ignore=shutil.ignore_patterns("__pycache__", "*.db"))
    db = os.path.join(dest, "estates_db.py")
    main = os.path.join(dest, "estates_main.py")
    with open(db, "a", encoding="utf-8") as fh:
        fh.write(CANARY_DEAD)
        fh.write(CANARY_DUP_A)
    with open(main, "a", encoding="utf-8") as fh:
        fh.write(CANARY_DUP_B)
        fh.write(CANARY_FORWARD)

    out = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--root", dest, "--json"],
        capture_output=True, text=True)
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        print("CANARY FAILED: tool did not produce JSON\n", out.stdout, out.stderr)
        return 2
    names = {(f["check"], f["kind"], f["name"]) for f in data["findings"]}
    checks = {
        "planted dead safety function (reverse)":
            any(c == "reverse" and k == "DEAD_SAFETY_MACHINERY"
                and n.endswith("canary_reconcile_orphans") for c, k, n in names),
        "planted duplicate judgement (duplicate)":
            any(c == "duplicate" and k == "DUPLICATE_JUDGEMENT"
                and "canary_outcome" in n for c, k, n in names),
        "planted missing name (forward)":
            any(c == "forward" and k == "missing-name" for c, k, n in names),
        "planted arity failure (forward)":
            any(c == "forward" and k == "signature" for c, k, n in names),
        "planted un-awaited coroutine (forward)":
            any(c == "forward" and k == "unawaited" for c, k, n in names),
    }
    print(f"canary tree: {dest}")
    for label, ok in checks.items():
        print(f"  {label:42s} caught: {ok}")
    if all(checks.values()):
        shutil.rmtree(tmp, ignore_errors=True)
        print("CANARY PASSED — a clean run from this tool is meaningful.")
        return 0
    print("CANARY FAILED — do not trust a clean run. Tree left at", dest)
    return 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="omit INFO findings")
    ap.add_argument("--strict", action="store_true",
                    help="duplicate judgements count as defects")
    ap.add_argument("--canary", action="store_true",
                    help="self-test: plant a dead function and a duplicate, require both")
    a = ap.parse_args()

    if a.canary:
        return canary(a.root)

    findings, stats = run(a.root, strict=a.strict)
    findings.sort(key=lambda f: (_ORDER[f.severity], f.check, f.name))
    if a.json:
        print(json.dumps({"stats": stats, "findings": [asdict(f) for f in findings]},
                         indent=2))
    else:
        report(findings, stats, quiet=a.quiet)
    return 1 if any(f.severity == SEV_DEFECT for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
