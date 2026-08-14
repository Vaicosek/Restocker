#!/usr/bin/env python3
"""check_docstrings.py — flag docstrings that claim a guarantee the code beside
them does not provide.

Four review rounds, four of these, one per round:

    round 1/2  `sweep_expired_holds` — "resumes at the exact hold it was on and
               never re-processes the ones it already released", over a query
               that re-selected from the top; the cursor was write-only.
    round 2    the escrow trigger — "Any writer ... is subject to it, because it
               lives in the schema", over a BEFORE UPDATE trigger; INSERT OR
               REPLACE and DELETE walked past it.
    round 3    `outcome_known_for` — "the ONE place that judgement lives, so
               estates_main's `_outcome_known` ... can never disagree", while
               `_outcome_known` hand-coded its own answer and never called it.
    round 4    `unpark_payout_row` — "the ONLY exit from a parked payout row",
               while `requeue_stuck_row` performed the same exit and the
               function refused a whole class of parked rows outright.

A docstring that promises a guarantee the code does not provide is worse than no
docstring: it is what the next reader trusts INSTEAD of reading the code. Every
one of the four was believed by a reviewer before it was caught.

WHAT THIS TOOL DOES NOT DO
--------------------------
It cannot verify semantics and does not pretend to. It is a targeted flagger:
it finds absolute-guarantee language (the CONFIG word list), extracts the
sub-claims a docstring makes about ITS OWN function, and tests only what is
structurally decidable from the AST and the SQL string literals:

    atomicity        "in one transaction" / "is atomic"  vs  the number of
                     transactions the body actually opens
    only-of-a-kind   "the ONLY exit/place/way"  vs  another function performing
                     the same write, or another module defining the same name
    totality         an UNQUALIFIED "the ONLY exit from X"  vs  guarded returns
                     that refuse to move some of the X it claims to cover
    named peer       "so `X` and this can never disagree"  vs  whether `X`
                     actually asks this function
    resumption       "resumes exactly where it stopped"  vs  whether anything
                     READS the progress marker the body writes
    coverage         "any writer is subject to it"  vs  which operations the
                     CREATE TRIGGER statements beside it actually cover
    call shape       "never called with X"  vs  the call sites
    mechanism        "guaranteed by `X`"  vs  whether `X` appears in the body
    interface drift  a frozen-interface document that says it was generated from
                     the source  vs  the docstring summaries it quotes

Every finding is one of:

    CONTRADICTED  a structural proof that the claim is false. THIS IS A DEFECT.
    UNVERIFIABLE  absolute language whose claim is semantic. Not a defect; it is
                  the set a human has to read. Reported so the count is honest.
    CONSISTENT    a checkable claim that the structure supports.

A CONSISTENT verdict is NOT a proof of correctness. It means the one structural
thing this tool can test came back clean.

WHAT IT READS, AND WHAT IT DOES NOT
----------------------------------
Function and method docstrings, and the `#:` comment block over a module-level
CONSTANT — estates_db states its judgement invariants there, and round 3's "this
is the ONE place that judgement lives" sat over `DEFINITE_REFUSAL_CODES`, not in
any docstring. MODULE docstrings are NOT audited: their claims are about a whole
file and this tool tests a body against its own prose. Inline `#` comments inside
a body are not audited either.

Usage
-----
    python3 check_docstrings.py                    # report over ./ (or --root)
    python3 check_docstrings.py --root /home/claude/build
    python3 check_docstrings.py --all              # list UNVERIFIABLE/CONSISTENT too
    python3 check_docstrings.py --modules a.py b.py    # audit a different set
    python3 check_docstrings.py --json
    python3 check_docstrings.py --canary           # self-test on the fixtures

Exit status: 0 no CONTRADICTED finding, 1 at least one, 2 the tool could not run.

Configuration is the CONFIG block below — the absolute-word list, the names that
open a transaction, the file exclusions. It is meant to be edited, not forked.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# CONFIG
# --------------------------------------------------------------------------- #

#: The modules whose docstrings are audited. Empty list = every .py in --root
#: that is not excluded. Named explicitly so a new module is a deliberate edit.
MODULES = [
    "estates_db.py",
    "estates_main.py",
    "ledger_client.py",
    "ledger_migrate.py",
    "ledger_v2.py",
]

#: Scanned for peers (same-name definitions, same-shaped writes, call sites) but
#: not audited themselves. A defect in a module nobody audits is still a peer.
PEER_ONLY = ["land_money_migrate.py"]

#: Never read: the tools and their fixtures. The fixture file exists to be
#: caught, so leaving it in a normal run would report four permanent defects.
EXCLUDE_FILES = {"check_docstrings.py", "check_docstrings_fixtures.py",
                 "check_docstrings_controls.py", "check_wiring.py"}

#: THE WORD LIST. A docstring sentence containing one of these is making an
#: absolute claim about the code beside it, and gets tested. Everything else is
#: prose and is not this tool's business.
ABSOLUTE_PATTERNS = [
    r"\balways\b", r"\bnever\b", r"\bcan(?:not|'t)\b", r"\bprovabl[ey]\b",
    r"\bthe ONLY\b", r"\bthe only\b", r"\bthe ONE\b", r"\bthe one place\b",
    r"\bguarantee[ds]?\b", r"\bguarantees\b", r"\bis atomic\b", r"\batomic(?:ally)?\b",
    r"\bin the same transaction\b", r"\bin one transaction\b",
    r"\bexactly once\b", r"\bat most once\b", r"\bno window\b",
    r"\bresumes? exactly where\b", r"\bresumes? (?:at|where|from)\b",
    r"\bimpossible\b", r"\bevery writer\b", r"\bany writer\b",
    r"\bcannot be bypassed\b", r"\bsingle source of truth\b",
]

#: Callables whose `with` block is a transaction. Used by the atomicity rule to
#: count how many transactions a body opens.
TX_OPENERS = {"db", "_tx", "tx", "transaction", "begin", "begin_immediate",
              "_conn", "connect", "_connection", "_db"}

#: Words that make a sentence counterfactual rather than a claim ("a cursor
#: would NOT resume where it stopped, it would skip live holds"). A rule that
#: cannot tell a claim from its refutation flags the fix as the bug.
NEGATION_CUES = [
    r"\bwould (?:not|have|be|skip|resume|need)\b", r"\bdeliberately no\b",
    r"\bthere is no\b", r"\bno (?:progress )?cursor\b", r"\bis not\b",
    r"\bdoes not\b", r"\bdo not\b", r"\bcould not\b", r"\bwas (?:not|never)\b",
    r"\bused to\b", r"\bbefore this\b", r"\buntil now\b", r"\bwithout this\b",
    r"\binstead of\b", r"\brather than\b", r"\bnot the same as\b",
]

#: Phrases that say the sentence is about code OUTSIDE this function — another
#: path, the caller, the rest of the module. The claim may be perfectly true and
#: is not this function's structure to contradict.
OTHER_SUBJECT_CUES = [
    r"\bnever reaches here\b", r"\bdoes not reach here\b", r"\bnot here\b",
    r"\belsewhere\b", r"\bin the caller\b", r"\bthe caller\b",
    r"\beverything else\b", r"\bother (?:paths|callers|endpoints)\b",
    r"\banother path\b", r"\bevery other\b",
]

#: Nouns that make an only-of-a-kind claim a claim about the DEFINITION ("the
#: only place", "the only implementation") rather than about behaviour ("the only
#: endpoint whose money moves outside our transaction"). A second definition of
#: the same name only contradicts the first kind.
DEFINITION_NOUNS = {"place", "implementation", "definition", "copy", "source",
                    "function", "module", "derivation", "home", "version"}

#: SQL tokens that name a progress marker for the resumption rule.
MARKER_WORDS = r"cursor|checkpoint|marker|last_(?:id|seen|row|key)|resume_from|progress_at"

#: Write operations a trigger-coverage claim has to account for.
WRITE_OPS = ("INSERT", "UPDATE", "DELETE")

#: Frozen-interface documents that quote a docstring summary under each name and
#: say of themselves that they were generated from the source. That header is
#: itself an absolute claim, and it is the one a CALLER reads instead of the
#: code: round 4's "the ONLY exit from a parked payout row" was corrected in
#: estates_db.py and left standing here, where the next integrator would find it.
INTERFACE_DOCS = {"estates_db": "ESTATES_DB_INTERFACE.md"}

VERDICT_CONTRADICTED = "CONTRADICTED"
VERDICT_UNVERIFIABLE = "UNVERIFIABLE"
VERDICT_CONSISTENT = "CONSISTENT"

#: How many UNVERIFIABLE lines to print without --all. The count is always
#: printed in full; this only bounds the listing.
UNVERIFIABLE_PREVIEW = 25


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #

@dataclass
class Finding:
    verdict: str
    rule: str
    name: str
    location: str
    claim: str
    detail: str
    evidence: list[str] = field(default_factory=list)

    def line(self) -> str:
        return f"[{self.verdict}] {self.rule}  {self.name}  ({self.location})"


@dataclass
class Func:
    module: str
    qualname: str
    lineno: int
    doc: str
    node: Any
    path: str
    audited: bool
    kind: str = "function"
    src: str = ""
    strings: list[str] = field(default_factory=list)
    calls: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    writes: set[tuple] = field(default_factory=set)
    selects: list[str] = field(default_factory=list)
    triggers: set[tuple] = field(default_factory=set)
    tx_opens: list[int] = field(default_factory=list)
    commits: int = 0

    @property
    def shortname(self) -> str:
        return self.qualname.rsplit(".", 1)[-1]


class ToolError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

_SQL_UPDATE = re.compile(r"UPDATE\s+([A-Za-z_][\w]*)\s+SET\s+(.+?)(?:\s+WHERE\b|\s*$)",
                         re.IGNORECASE | re.DOTALL)
_SQL_ASSIGN = re.compile(r"([A-Za-z_][\w]*)\s*=\s*'([^']*)'")
_SQL_INSERT = re.compile(r"INSERT(?:\s+OR\s+\w+)?\s+INTO\s+([A-Za-z_][\w]*)", re.IGNORECASE)
_SQL_SELECT = re.compile(r"\bSELECT\b", re.IGNORECASE)
_SQL_TRIGGER = re.compile(
    r"CREATE\s+TRIGGER(?:\s+IF\s+NOT\s+EXISTS)?\s+([\w.]+)\s+"
    r"(BEFORE|AFTER|INSTEAD\s+OF)\s+(INSERT|UPDATE|DELETE)(?:\s+OF\s+[\w,\s]+?)?\s+ON\s+([\w.]+)",
    re.IGNORECASE)


def _body_nodes(node: ast.AST) -> list[ast.AST]:
    """A function's body WITHOUT its docstring.

    This matters more than it looks: the docstring is a string constant like any
    other, so a naive walk finds the word "cursor" in a docstring that says
    "there is deliberately no cursor" and reports the function for writing a
    marker it does not write. The prose is the thing under test; it is never
    evidence.
    """
    body = list(getattr(node, "body", []))
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    return body


def _strings_in(node: ast.AST) -> list[str]:
    out = []
    nodes = _body_nodes(node) if isinstance(
        node, (ast.FunctionDef, ast.AsyncFunctionDef)) else [node]
    for sub in [s for n in nodes for s in ast.walk(n)]:
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.append(sub.value)
        elif isinstance(sub, ast.JoinedStr):
            parts = [v.value for v in sub.values
                     if isinstance(v, ast.Constant) and isinstance(v.value, str)]
            if parts:
                out.append(" ".join(parts))
    return out


def _writes_in(strings: Iterable[str]) -> set[tuple]:
    """(table, column, value) for every `UPDATE t SET c='v'` in a string literal.

    Only literal assignments are recorded. `SET attempts=attempts+1` is not a
    state transition anyone claims to be the only source of.
    """
    out: set[tuple] = set()
    for s in strings:
        for m in _SQL_UPDATE.finditer(s):
            table = m.group(1).lower()
            for a in _SQL_ASSIGN.finditer(m.group(2)):
                out.add((table, a.group(1).lower(), a.group(2)))
    return out


def _triggers_in(strings: Iterable[str]) -> set[tuple]:
    out: set[tuple] = set()
    for s in strings:
        for m in _SQL_TRIGGER.finditer(s):
            out.add((m.group(3).upper(), m.group(4).lower()))
    return out


def _calls_and_names(node: ast.AST) -> tuple[set[str], set[str]]:
    calls: set[str] = set()
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Name):
                calls.add(f.id)
            elif isinstance(f, ast.Attribute):
                calls.add(f.attr)
        if isinstance(sub, ast.Name):
            names.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            names.add(sub.attr)
    return calls, names


def _tx_opens(node: ast.AST) -> tuple[list[int], int]:
    """Line numbers of the `with <opener>()` statements in a body, and the number
    of explicit `.commit()` calls. Two of either means two transactions."""
    opens: list[int] = []
    commits = 0
    for sub in ast.walk(node):
        if isinstance(sub, (ast.With, ast.AsyncWith)):
            for item in sub.items:
                ctx = item.context_expr
                fn = None
                if isinstance(ctx, ast.Call):
                    if isinstance(ctx.func, ast.Name):
                        fn = ctx.func.id
                    elif isinstance(ctx.func, ast.Attribute):
                        fn = ctx.func.attr
                if fn and fn in TX_OPENERS:
                    opens.append(sub.lineno)
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                and sub.func.attr == "commit":
            commits += 1
    return opens, commits


def collect(path: str, audited: bool) -> list[Func]:
    text = open(path, encoding="utf-8").read()
    try:
        tree = ast.parse(text, path)
    except SyntaxError as e:
        raise ToolError(f"{path}: {e}") from e
    mod = os.path.splitext(os.path.basename(path))[0]
    out: list[Func] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{child.name}"
                doc = ast.get_docstring(child) or ""
                strings = _strings_in(child)
                calls, names = _calls_and_names(child)
                opens, commits = _tx_opens(child)
                out.append(Func(
                    module=mod, qualname=qual, lineno=child.lineno, doc=doc,
                    node=child, path=path, audited=audited,
                    src=ast.get_source_segment(text, child) or "",
                    strings=strings, calls=calls, names=names,
                    writes=_writes_in(strings),
                    selects=[s for s in strings if _SQL_SELECT.search(s)],
                    triggers=_triggers_in(strings),
                    tx_opens=opens, commits=commits))
                walk(child, f"{qual}.")
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")

    walk(tree, "")

    # Module-level constants, documented in the `#:` block above them. estates_db
    # states the judgement invariants THERE, not in a docstring — round 3's "this
    # is the ONE place that judgement lives" sat over DEFINITE_REFUSAL_CODES, so
    # a checker that only read function docstrings would have walked past the
    # exact defect it exists to find.
    lines = text.splitlines()
    for node in tree.body:
        targets = ([t for t in node.targets] if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        for t in targets:
            if not isinstance(t, ast.Name) or not re.match(r"^[A-Z_][A-Z0-9_]*$", t.id):
                continue
            doc = _comment_block(lines, node.lineno)
            if not doc:
                continue
            val = node.value
            strings = _strings_in(val) if val is not None else []
            calls, names = (_calls_and_names(val) if val is not None
                            else (set(), set()))
            out.append(Func(
                module=mod, qualname=t.id, lineno=node.lineno, doc=doc, node=node,
                path=path, audited=audited, kind="constant",
                src=ast.get_source_segment(text, node) or "",
                strings=strings, calls=calls, names=names,
                writes=_writes_in(strings),
                selects=[s for s in strings if _SQL_SELECT.search(s)],
                triggers=_triggers_in(strings)))
    return out


def _comment_block(lines: list[str], lineno: int) -> str:
    """The `#:`/`#` block immediately above a definition, as its documentation."""
    out: list[str] = []
    i = lineno - 2
    while i >= 0:
        s = lines[i].strip()
        if s.startswith("#"):
            out.append(s.lstrip("#:").lstrip("#").strip())
            i -= 1
            continue
        break
    return "\n".join(reversed(out)).strip()


class Index:
    """Everything a rule needs to look outside the function it is judging."""

    def __init__(self, funcs: list[Func], sources: dict[str, str]):
        self.funcs = funcs
        self.sources = sources
        self.by_short: dict[str, list[Func]] = {}
        for f in funcs:
            self.by_short.setdefault(f.shortname, []).append(f)
        self.write_owners: dict[tuple, list[Func]] = {}
        for f in funcs:
            for w in f.writes:
                self.write_owners.setdefault(w, []).append(f)
        self.module_consts: dict[str, dict[str, list[str]]] = {}
        self.callsites: dict[str, list[tuple[str, int, set[str], list[str]]]] = {}
        self._index_module_level(sources)

    def _index_module_level(self, sources: dict[str, str]) -> None:
        for path, text in sources.items():
            mod = os.path.splitext(os.path.basename(path))[0]
            tree = ast.parse(text, path)
            consts: dict[str, list[str]] = {}
            for node in tree.body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = (node.targets if isinstance(node, ast.Assign)
                               else [node.target])
                    val = node.value
                    if val is None:
                        continue
                    for t in targets:
                        if isinstance(t, ast.Name):
                            consts[t.id] = _strings_in(val)
            self.module_consts[mod] = consts
            for sub in ast.walk(tree):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    nm = (f.id if isinstance(f, ast.Name)
                          else f.attr if isinstance(f, ast.Attribute) else None)
                    if not nm:
                        continue
                    kw = {k.arg for k in sub.keywords if k.arg}
                    lits = [repr(a.value) for a in sub.args
                            if isinstance(a, ast.Constant)]
                    lits += [repr(k.value.value) for k in sub.keywords
                             if isinstance(k.value, ast.Constant)]
                    self.callsites.setdefault(nm, []).append(
                        (f"{mod}.py", sub.lineno, kw, lits))

    def peers_named(self, name: str, exclude: Func) -> list[Func]:
        return [f for f in self.by_short.get(name, []) if f is not exclude]

    def const_strings(self, module: str, name: str) -> list[str]:
        return self.module_consts.get(module, {}).get(name, [])


# --------------------------------------------------------------------------- #
# Claim extraction
# --------------------------------------------------------------------------- #

_ABS = [re.compile(p, re.IGNORECASE) for p in ABSOLUTE_PATTERNS]
_NEG = [re.compile(p, re.IGNORECASE) for p in NEGATION_CUES]
_OTHER = [re.compile(p, re.IGNORECASE) for p in OTHER_SUBJECT_CUES]
_IDENT = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)(?:\(\))?`|\b([A-Za-z_]\w*)\(\)")


def sentences(doc: str) -> list[str]:
    """Docstrings are prose with bullets and code blocks. Split on sentence ends
    and on blank lines / bullet starts, keep anything with words in it."""
    flat = re.sub(r"\s*\n\s*", " ", doc.strip())
    parts = re.split(r"(?<=[.!?;])\s+(?=[A-Z`*(\[])|\s+\*\s+", flat)
    return [p.strip() for p in parts if p and len(p.strip()) > 3]


def absolute_words(sentence: str) -> list[str]:
    hits = []
    for rx in _ABS:
        m = rx.search(sentence)
        if m:
            hits.append(m.group(0))
    return hits


def is_negated(sentence: str) -> bool:
    return any(rx.search(sentence) for rx in _NEG)


def is_about_other_code(sentence: str) -> bool:
    return any(rx.search(sentence) for rx in _OTHER)


def idents_in(sentence: str) -> list[str]:
    out = []
    for m in _IDENT.finditer(sentence):
        name = m.group(1) or m.group(2)
        if name:
            out.append(name.split(".")[-1])
    return out


# --------------------------------------------------------------------------- #
# Rules. Each returns a Finding or None. A rule that cannot decide returns an
# UNVERIFIABLE finding rather than silence, so the count stays honest.
# --------------------------------------------------------------------------- #

RX_ATOMIC = re.compile(
    r"\bis atomic\b|\batomic(?:ally)?\b|\bin (?:one|a single|the same) transaction\b"
    r"|\bone transaction\b|\bsame transaction\b", re.IGNORECASE)
RX_ONLY = re.compile(
    r"\bthe ONLY\b|\bthe only (?:exit|way|path|place|route|caller|writer|thing|"
    r"function|module|code|point|source)\b|\bthe ONE place\b|\bonly place\b"
    r"|\bsingle source of truth\b", re.IGNORECASE)
RX_AGREE = re.compile(
    r"\bnever disagree\b|\bthe ONE place\b|\bsingle source of truth\b"
    r"|\bcannot (?:disagree|drift|diverge)\b|\bcan never (?:disagree|drift)\b",
    re.IGNORECASE)
RX_RESUME = re.compile(
    r"\bresumes? (?:exactly )?(?:at|where|from)\b|\bpicks? up where\b"
    r"|\bnever re-?process(?:es)?\b|\bcontinues? (?:from|where)\b"
    r"|\bwhere it (?:stopped|left off)\b", re.IGNORECASE)
RX_COVERAGE = re.compile(
    r"\bany writer\b|\bevery writer\b|\bevery write\b|\bcannot be bypassed\b"
    r"|\bno way to bypass\b|\bschema[- ]wide\b|\bevery path\b"
    r"|\blives in the schema\b|\bevery statement\b", re.IGNORECASE)
RX_NEVERCALL = re.compile(
    r"\bnever called with\b|\bcallers never pass\b|\bis never passed\b"
    r"|\bnever called from\b|\bnever invoked with\b", re.IGNORECASE)
RX_MECHANISM = re.compile(
    r"(?:because|via|through|thanks to|enforced by|guarded by|provided by|"
    r"by way of)\s+`([A-Za-z_][\w.]*)`", re.IGNORECASE)


def rule_atomicity(f: Func, s: str, idx: Index) -> Finding | None:
    if not RX_ATOMIC.search(s) or is_negated(s):
        return None
    loc = f"{f.module}.py:{f.lineno}"
    if len(f.tx_opens) > 1:
        return Finding(
            VERDICT_CONTRADICTED, "atomicity", f"{f.module}.{f.qualname}", loc, s,
            f"the docstring says one transaction; the body opens "
            f"{len(f.tx_opens)} (lines {', '.join(str(n) for n in f.tx_opens)}). "
            f"Work split across two transactions is two outcomes: the first can "
            f"commit and the second can fail.",
            [f"transaction opened at line {n}" for n in f.tx_opens])
    if f.commits > 1:
        return Finding(
            VERDICT_CONTRADICTED, "atomicity", f"{f.module}.{f.qualname}", loc, s,
            f"the docstring says one transaction; the body calls .commit() "
            f"{f.commits} times.", [])
    # "in the same transaction as `X`": X must be called inside the with-block.
    outside = []
    for ident in idents_in(s):
        if ident in f.calls and f.tx_opens:
            inside = _called_inside_with(f.node, ident)
            if not inside:
                outside.append(ident)
    if outside:
        return Finding(
            VERDICT_CONTRADICTED, "atomicity", f"{f.module}.{f.qualname}", loc, s,
            f"the claim names {', '.join('`'+o+'`' for o in outside)} as part of "
            f"the same transaction, but the call is outside every transaction "
            f"block in this body.", [])
    if not f.tx_opens and not f.commits:
        return Finding(
            VERDICT_UNVERIFIABLE, "atomicity", f"{f.module}.{f.qualname}", loc, s,
            "claims atomicity but opens no transaction here — the transaction is "
            "the caller's, which this tool cannot follow.", [])
    return Finding(VERDICT_CONSISTENT, "atomicity", f"{f.module}.{f.qualname}",
                   loc, s, "exactly one transaction is opened in this body.", [])


def _called_inside_with(node: ast.AST, ident: str) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, (ast.With, ast.AsyncWith)):
            for inner in ast.walk(sub):
                if isinstance(inner, ast.Call):
                    fn = inner.func
                    nm = (fn.id if isinstance(fn, ast.Name)
                          else fn.attr if isinstance(fn, ast.Attribute) else None)
                    if nm == ident:
                        return True
    return False


def rule_only_of_a_kind(f: Func, s: str, idx: Index) -> Finding | None:
    if not RX_ONLY.search(s) or is_negated(s):
        return None
    loc = f"{f.module}.py:{f.lineno}"
    doc_idents = set(idents_in(f.doc))
    # (a) another function performs the same literal state write
    for w in sorted(f.writes):
        for peer in idx.write_owners.get(w, []):
            if peer is f or peer.qualname == f.qualname:
                continue
            if peer.shortname in doc_idents or peer.shortname in f.calls:
                continue          # the docstring already names it, or delegates
            return Finding(
                VERDICT_CONTRADICTED, "only-of-a-kind", f"{f.module}.{f.qualname}",
                loc, s,
                f"claims to be the only one, but {peer.module}.{peer.qualname} "
                f"({peer.module}.py:{peer.lineno}) performs the same write "
                f"`UPDATE {w[0]} SET {w[1]}='{w[2]}'`, is not called from here, "
                f"and is not named in the docstring.",
                [f"shared write: UPDATE {w[0]} SET {w[1]}='{w[2]}'",
                 f"peer: {peer.module}.py:{peer.lineno} {peer.qualname}"])
    # (b) another module defines the same name. Only a claim about the
    # DEFINITION can be contradicted this way: "the only endpoint whose money
    # moves outside our transaction" is a claim about behaviour, and the client
    # library's same-named wrapper does not touch it.
    noun_m = re.search(r"\bthe (?:ONLY|only|ONE|one)\s+([A-Za-z_]+)", s)
    noun = (noun_m.group(1).lower() if noun_m else "")
    for peer in idx.peers_named(f.shortname, f) if noun in DEFINITION_NOUNS else []:
        if peer.module == f.module:
            continue
        if peer.shortname in f.calls or f.shortname in peer.calls:
            continue
        return Finding(
            VERDICT_CONTRADICTED, "only-of-a-kind", f"{f.module}.{f.qualname}",
            loc, s,
            f"claims to be the only one, but {peer.module}.py:{peer.lineno} "
            f"defines a second `{f.shortname}` and neither calls the other — "
            f"nothing makes the two agree.",
            [f"second definition: {peer.module}.py:{peer.lineno}"])
    return Finding(
        VERDICT_UNVERIFIABLE, "only-of-a-kind", f"{f.module}.{f.qualname}", loc, s,
        "an only-of-a-kind claim with no same-shaped peer found. Whether it is "
        "the only one in the sense the sentence means is semantic.", [])


def _reaches(start: Func, target: str, idx: Index, depth: int = 2) -> bool:
    """Does `start` reach the name `target`, directly or one call away?

    A constant states its invariant over itself ("this is the ONE place that
    judgement lives"), but the peer that must agree with it does not read the
    constant — it calls the accessor that reads it. One hop is the difference
    between "asks" and "keeps its own copy".
    """
    frontier = [start]
    seen: set[str] = set()
    for _ in range(depth):
        nxt: list[Func] = []
        for p in frontier:
            if target in p.calls or target in p.names:
                return True
            for nm in sorted(p.calls):
                for cand in idx.by_short.get(nm, [])[:4]:
                    key = f"{cand.module}.{cand.qualname}"
                    if key not in seen:
                        seen.add(key)
                        nxt.append(cand)
        frontier = nxt[:50]
    return False


#: The lookahead is glued to the noun ON PURPOSE: written as `\s*(?!that)` the
#: engine backtracks `\s*` to zero width and "the only exit that is a RETRY" —
#: a qualified, true sentence — matches as if it were unqualified.
RX_TOTALITY = re.compile(
    r"\bthe (?:ONLY|only) (exit|way|path|route|means|escape|door)\b"
    r"(?!\s+(?:that|which|when|for which)\b)", re.IGNORECASE)


def rule_totality_refusal(f: Func, s: str, idx: Index) -> Finding | None:
    """"The ONLY exit from a parked payout row" — over a body that refuses some.

    Round 4's docstring said exactly that while an early return declined every
    row of a `market_reverse` run, which is the one run kind whose parking is
    designed in. For those rows the function was not the only exit, it was no
    exit at all, and the sentence is what stopped anyone looking. A QUALIFIED
    claim ("the only exit that is a RETRY") is a different sentence and is not
    tested here — the negative lookahead is doing that work.
    """
    if not RX_TOTALITY.search(s) or is_negated(s):
        return None
    loc = f"{f.module}.py:{f.lineno}"
    write_lines = []
    for sub in ast.walk(f.node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and \
                re.search(r"\b(UPDATE|INSERT|DELETE)\b", sub.value, re.IGNORECASE):
            write_lines.append(sub.lineno)
    if not write_lines:
        return Finding(
            VERDICT_UNVERIFIABLE, "totality", f"{f.module}.{f.qualname}", loc, s,
            "an unqualified only-exit claim in a body that performs no write "
            "here; what it is the only exit from is not decidable.", [])
    first_write = min(write_lines)
    refusals = []
    for sub in ast.walk(f.node):
        if isinstance(sub, ast.If):
            for r in ast.walk(sub):
                if isinstance(r, ast.Return) and r.lineno < first_write:
                    refusals.append(r.lineno)
    if refusals:
        return Finding(
            VERDICT_CONTRADICTED, "totality", f"{f.module}.{f.qualname}", loc, s,
            f"the claim is unqualified — the only exit, full stop — but the body "
            f"returns without performing the write on {len(set(refusals))} "
            f"guarded path(s) (lines {', '.join(str(n) for n in sorted(set(refusals)))}), "
            f"before the first write at line {first_write}. For the rows those "
            f"guards catch this is not the only exit; it is no exit. Either "
            f"qualify the sentence or name the class it refuses.",
            [f"guarded return at line {n}" for n in sorted(set(refusals))])
    return Finding(
        VERDICT_CONSISTENT, "totality", f"{f.module}.{f.qualname}", loc, s,
        "no guarded return precedes the write: every input reaches it.", [])


def rule_named_peer(f: Func, s: str, idx: Index) -> Finding | None:
    if not RX_AGREE.search(s) or is_negated(s):
        return None
    loc = f"{f.module}.py:{f.lineno}"
    for ident in idents_in(s):
        if ident == f.shortname:
            continue
        peers = idx.peers_named(ident, f)
        if not peers:
            continue
        for peer in peers:
            asks_us = _reaches(peer, f.shortname, idx)
            we_ask = (peer.shortname in f.calls) or (peer.shortname in f.names)
            if asks_us or we_ask:
                return Finding(
                    VERDICT_CONSISTENT, "named-peer", f"{f.module}.{f.qualname}",
                    loc, s,
                    f"{peer.module}.{peer.qualname} does reference this function, "
                    f"so the agreement is enforced by delegation.", [])
            return Finding(
                VERDICT_CONTRADICTED, "named-peer", f"{f.module}.{f.qualname}",
                loc, s,
                f"the claim names `{ident}` as the thing that can never disagree "
                f"with this, but {peer.module}.{peer.qualname} "
                f"({peer.module}.py:{peer.lineno}) never calls this function and "
                f"this function never calls it — the two are independent "
                f"implementations of one judgement.",
                [f"peer: {peer.module}.py:{peer.lineno} {peer.qualname}"])
    return Finding(
        VERDICT_UNVERIFIABLE, "named-peer", f"{f.module}.{f.qualname}", loc, s,
        "agreement claim whose peer this tool could not resolve to a definition.",
        [])


def rule_resumption(f: Func, s: str, idx: Index) -> Finding | None:
    if not RX_RESUME.search(s) or is_negated(s):
        return None
    loc = f"{f.module}.py:{f.lineno}"
    marker_rx = re.compile(MARKER_WORDS, re.IGNORECASE)
    markers = sorted({m.group(0) for lit in f.strings for m in
                      re.finditer(r"[A-Za-z_]*(?:%s)[A-Za-z_]*" % MARKER_WORDS, lit,
                                  re.IGNORECASE)})
    markers += [n for n in sorted(f.names) if marker_rx.search(n)]
    if markers:
        read = [lit for lit in f.selects if marker_rx.search(lit)]
        if not read:
            return Finding(
                VERDICT_CONTRADICTED, "resumption", f"{f.module}.{f.qualname}",
                loc, s,
                f"the body writes a progress marker ({', '.join(sorted(set(markers))[:3])}) "
                f"and no SELECT in this body reads it: the candidate query "
                f"re-selects from the top every pass, so the run does not resume "
                f"where it stopped — whatever safety it has comes from somewhere "
                f"else, and the marker only looks load-bearing.",
                [f"marker written: {m}" for m in sorted(set(markers))[:3]])
        return Finding(
            VERDICT_CONSISTENT, "resumption", f"{f.module}.{f.qualname}", loc, s,
            "a progress marker is both written and read by a SELECT in this body.",
            [])
    claim_first = any(re.search(r"\bWHERE\b.*=\s*'", lit, re.IGNORECASE | re.DOTALL)
                      for lit in f.selects) and bool(f.writes)
    if claim_first:
        return Finding(
            VERDICT_CONSISTENT, "resumption", f"{f.module}.{f.qualname}", loc, s,
            "no marker, but the body selects on a state column it then writes — "
            "resumption by claim, which is the shape that actually works.", [])
    return Finding(
        VERDICT_UNVERIFIABLE, "resumption", f"{f.module}.{f.qualname}", loc, s,
        "a resumption claim with no marker and no state query in this body; the "
        "resumption, if any, is in the caller.", [])


def rule_coverage(f: Func, s: str, idx: Index) -> Finding | None:
    if not RX_COVERAGE.search(s) or is_negated(s):
        return None
    loc = f"{f.module}.py:{f.lineno}"
    trigs = set(f.triggers)
    if not trigs:                      # DDL may live in a module constant
        for n in sorted(f.names):
            trigs |= _triggers_in(idx.const_strings(f.module, n))
    if not trigs:
        return Finding(
            VERDICT_UNVERIFIABLE, "coverage", f"{f.module}.{f.qualname}", loc, s,
            "a coverage claim with no CREATE TRIGGER statement reachable from "
            "this body; what enforces it is not decidable here.", [])
    tables = {t for _, t in trigs}
    missing_by_table = {}
    for table in sorted(tables):
        covered = {op for op, t in trigs if t == table}
        missing = [op for op in WRITE_OPS if op not in covered]
        if missing:
            missing_by_table[table] = missing
    if missing_by_table:
        parts = [f"{t}: no {'/'.join(m)} trigger" for t, m in missing_by_table.items()]
        return Finding(
            VERDICT_CONTRADICTED, "coverage", f"{f.module}.{f.qualname}", loc, s,
            f"the claim covers every writer; the DDL beside it does not: "
            f"{'; '.join(parts)}. In SQLite `INSERT OR REPLACE` is DELETE+INSERT "
            f"and fires neither an UPDATE trigger nor an UPDATE OF trigger, so a "
            f"writer that uses it walks straight past this guard.",
            [f"installed: {op} ON {t}" for op, t in sorted(trigs)])
    return Finding(
        VERDICT_CONSISTENT, "coverage", f"{f.module}.{f.qualname}", loc, s,
        f"INSERT, UPDATE and DELETE are all covered on {', '.join(sorted(tables))}.",
        [])


def rule_never_called_with(f: Func, s: str, idx: Index) -> Finding | None:
    if not RX_NEVERCALL.search(s):
        return None
    loc = f"{f.module}.py:{f.lineno}"
    tail = s[RX_NEVERCALL.search(s).end():]
    wanted = [t for t in idents_in(tail)] + re.findall(r"`([\w=']+)`", tail)
    sites = idx.callsites.get(f.shortname, [])
    if not sites:
        return Finding(
            VERDICT_UNVERIFIABLE, "call-shape", f"{f.module}.{f.qualname}", loc, s,
            "no call site of this function was found, so the claim about how it "
            "is called cannot be tested (and nothing exercises it).", [])
    for (where, line, kwargs, lits) in sites:
        for w in wanted:
            if w in kwargs or repr(w) in lits or f"'{w}'" in lits:
                return Finding(
                    VERDICT_CONTRADICTED, "call-shape", f"{f.module}.{f.qualname}",
                    loc, s,
                    f"the docstring says it is never called with `{w}`; "
                    f"{where}:{line} does exactly that.",
                    [f"call site: {where}:{line}"])
    return Finding(
        VERDICT_CONSISTENT, "call-shape", f"{f.module}.{f.qualname}", loc, s,
        f"{len(sites)} call site(s) checked; none passes what the docstring "
        f"excludes.", [])


def rule_mechanism(f: Func, s: str, idx: Index) -> Finding | None:
    m = RX_MECHANISM.search(s)
    if not m or is_negated(s):
        return None
    ident = m.group(1).split(".")[-1]
    loc = f"{f.module}.py:{f.lineno}"
    if is_about_other_code(s):
        return Finding(
            VERDICT_UNVERIFIABLE, "mechanism", f"{f.module}.{f.qualname}", loc, s,
            f"the sentence credits `{ident}` for a path that explicitly runs "
            f"somewhere else ('never reaches here' and its kin), so this body is "
            f"not where the claim would be contradicted.", [])
    if ident in f.calls or ident in f.names or ident == f.shortname:
        return Finding(
            VERDICT_CONSISTENT, "mechanism", f"{f.module}.{f.qualname}", loc, s,
            f"`{ident}` is named as the mechanism and does appear in the body.", [])
    if not idx.peers_named(ident, f) and ident not in idx.callsites:
        return Finding(
            VERDICT_UNVERIFIABLE, "mechanism", f"{f.module}.{f.qualname}", loc, s,
            f"`{ident}` does not resolve to a definition in the scanned modules — "
            f"it may be a table, a column or a name in another service.", [])
    return Finding(
        VERDICT_CONTRADICTED, "mechanism", f"{f.module}.{f.qualname}", loc, s,
        f"the guarantee is credited to `{ident}`, which is defined in the tree "
        f"but appears nowhere in this function's body: whatever holds the "
        f"guarantee up, it is not the thing the docstring points at.",
        [f"`{ident}` defined at " +
         ", ".join(f"{p.module}.py:{p.lineno}" for p in idx.peers_named(ident, f)[:3])])


RULES = [rule_atomicity, rule_only_of_a_kind, rule_totality_refusal,
         rule_named_peer, rule_resumption, rule_coverage, rule_never_called_with,
         rule_mechanism]

_DOC_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")


def interface_drift(root: str, funcs: list[Func],
                    docs: dict[str, str] | None = None) -> list[Finding]:
    """Check each frozen-interface document against the docstrings it quotes.

    The document states it is generated from the source and is the contract, so
    a summary that no longer matches the docstring is that claim contradicted —
    and it is contradicted in the direction that matters, because the caller
    reads the contract and never opens the module.
    """
    out: list[Finding] = []
    for module, docname in (docs if docs is not None else INTERFACE_DOCS).items():
        path = os.path.join(root, docname)
        if not os.path.exists(path):
            continue
        first: dict[str, str] = {}
        for f in funcs:
            if f.module == module and f.kind == "function" and "." not in f.qualname:
                flat = " ".join(f.doc.split())
                first[f.qualname] = flat
        lines = open(path, encoding="utf-8").read().splitlines()
        for i, line in enumerate(lines):
            m = _DOC_DEF.match(line)
            if not m:
                continue
            name = m.group(1)
            quoted: list[str] = []
            j = i + 1
            while j < len(lines) and lines[j].strip().startswith("#"):
                quoted.append(lines[j].strip().lstrip("#").strip())
                j += 1
            summary = " ".join(quoted).strip()
            src = first.get(name)
            if not summary or src is None:
                continue
            if src.startswith(summary[:len(summary)]) or (
                    summary and src.startswith(summary.rstrip("."))):
                continue
            out.append(Finding(
                VERDICT_CONTRADICTED, "interface-drift", f"{module}.{name}",
                f"{docname}:{i + 1}", summary,
                f"the frozen interface says it was generated from the source and "
                f"is the contract, and this summary is not what the docstring "
                f"says now: source opens \"{src[:100]}\". A caller reads this "
                f"file, not the module.",
                [f"source: {module}.py"]))
    return out

_SEVERITY = {VERDICT_CONTRADICTED: 0, VERDICT_UNVERIFIABLE: 1, VERDICT_CONSISTENT: 2}


def audit(funcs: list[Func], idx: Index) -> tuple[list[Finding], dict]:
    findings: list[Finding] = []
    stats = {"functions": 0, "with_docstring": 0, "with_absolute_language": 0,
             "claims_tested": 0}
    for f in funcs:
        if not f.audited:
            continue
        stats["functions"] += 1
        if not f.doc:
            continue
        stats["with_docstring"] += 1
        claims = [s for s in sentences(f.doc) if absolute_words(s)]
        if not claims:
            continue
        stats["with_absolute_language"] += 1
        per_func: list[Finding] = []
        for s in claims:
            stats["claims_tested"] += 1
            hits = [r(f, s, idx) for r in RULES]
            hits = [h for h in hits if h is not None]
            if hits:
                per_func.extend(hits)
            else:
                per_func.append(Finding(
                    VERDICT_UNVERIFIABLE, "semantic", f"{f.module}.{f.qualname}",
                    f"{f.module}.py:{f.lineno}", s,
                    "absolute language (" + ", ".join(sorted(set(absolute_words(s))))
                    + ") whose claim is semantic: no structural test applies.", []))
        # One line per function per verdict class keeps the report readable.
        best = min(_SEVERITY[h.verdict] for h in per_func)
        for h in per_func:
            if _SEVERITY[h.verdict] == best:
                findings.append(h)
    findings.sort(key=lambda h: (_SEVERITY[h.verdict], h.name))
    return findings, stats


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

def load(root: str, modules: list[str], peers: list[str]) -> tuple[list[Func], Index]:
    funcs: list[Func] = []
    sources: dict[str, str] = {}
    wanted = [(m, True) for m in modules] + [(p, False) for p in peers]
    if not modules:
        wanted = [(fn, True) for fn in sorted(os.listdir(root))
                  if fn.endswith(".py") and fn not in EXCLUDE_FILES]
    for fn, audited in wanted:
        if fn in EXCLUDE_FILES:
            continue
        path = os.path.join(root, fn)
        if not os.path.exists(path):
            raise ToolError(f"missing module: {path}")
        sources[path] = open(path, encoding="utf-8").read()
        funcs.extend(collect(path, audited))
    if not funcs:
        raise ToolError(f"no modules found under {root}")
    return funcs, Index(funcs, sources)


def report(findings: list[Finding], stats: dict, show_all: bool,
           quiet: bool) -> int:
    bad = [f for f in findings if f.verdict == VERDICT_CONTRADICTED]
    unv = [f for f in findings if f.verdict == VERDICT_UNVERIFIABLE]
    con = [f for f in findings if f.verdict == VERDICT_CONSISTENT]
    if not quiet:
        print("=" * 78)
        print("check_docstrings — absolute claims tested against the code beside them")
        print("=" * 78)
        print(f"functions audited: {stats['functions']}   with docstring: "
              f"{stats['with_docstring']}   with absolute language: "
              f"{stats['with_absolute_language']}   claims tested: "
              f"{stats['claims_tested']}")
        print(f"CONTRADICTED {len(bad)}   UNVERIFIABLE {len(unv)}   "
              f"CONSISTENT {len(con)}")
    for group, items, always in ((VERDICT_CONTRADICTED, bad, True),
                                 (VERDICT_UNVERIFIABLE, unv, False),
                                 (VERDICT_CONSISTENT, con, False)):
        if not items or (not always and not show_all and quiet):
            continue
        if not always and not show_all:
            if quiet:
                continue
            print("\n" + "-" * 78)
            print(f"{group}  ({len(items)})   — showing "
                  f"{min(len(items), UNVERIFIABLE_PREVIEW)}; --all for the rest")
            print("-" * 78)
            for h in items[:UNVERIFIABLE_PREVIEW]:
                print(f"[{h.verdict}] {h.rule:16s} {h.name} ({h.location})")
            continue
        print("\n" + "-" * 78)
        print(f"{group}  ({len(items)})")
        print("-" * 78)
        for h in items:
            print(f"\n{h.line()}")
            print(f'    claim: "{h.claim.strip()}"')
            print(f"    {h.detail}")
            for e in h.evidence:
                print(f"    - {e}")
    if not quiet:
        print("\n" + "=" * 78)
        print(f"RESULT: {len(bad)} CONTRADICTED (defects), {len(unv)} "
              f"UNVERIFIABLE (for a human), {len(con)} CONSISTENT")
    return 1 if bad else 0


# --------------------------------------------------------------------------- #
# Canary — the four historical defects, plus controls that must NOT fire
# --------------------------------------------------------------------------- #

CANARY_MUST_CATCH = {
    "check_docstrings_fixtures.fixture_sweep_expired_holds":
        "round 1/2 S11: resume claim over a write-only cursor",
    "check_docstrings_fixtures.fixture_install_escrow_guard":
        "round 2 N3: 'any writer' over a BEFORE UPDATE trigger only",
    "check_docstrings_fixtures.DEFINITE_REFUSAL_CODES":
        "round 3 R3-1: 'the ONE place' (a `#:` block over a constant) with a "
        "peer that never asks it",
    "check_docstrings_fixtures.fixture_unpark_payout_row":
        "round 4: 'the ONLY exit' with a peer performing the same exit",
}

#: The corrected shape of each of the four, audited in a SEPARATE pass. A fixed
#: docstring and its broken original in one file are peers of each other, and the
#: tool would be right to report the fixed one — so they do not share a file.
CANARY_CONTROL_FILE = "check_docstrings_controls.py"


def _audit_file(path: str) -> list[Finding]:
    text = open(path, encoding="utf-8").read()
    funcs = collect(path, audited=True)
    findings, _ = audit(funcs, Index(funcs, {path: text}))
    return findings


def _canary_interface_drift() -> bool:
    """Plant a frozen-interface summary that the source no longer says, in a temp
    tree, and require the drift check to report it."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "iface_mod.py"), "w", encoding="utf-8") as fh:
            fh.write('def unpark_payout_row(row_id):\n'
                     '    """`failed -> pending`. This is the only exit that is a '
                     'RETRY."""\n    return "pending"\n')
        with open(os.path.join(d, "IFACE.md"), "w", encoding="utf-8") as fh:
            fh.write("# Generated by AST from the actual source. This file is the "
                     "contract.\n"
                     "def unpark_payout_row(row_id) -> str\n"
                     "    # `failed -> pending`, the ONLY exit from a parked "
                     "payout row.\n")
        funcs = collect(os.path.join(d, "iface_mod.py"), audited=True)
        found = interface_drift(d, funcs, {"iface_mod": "IFACE.md"})
    return any(f.verdict == VERDICT_CONTRADICTED for f in found)


def canary(root: str) -> int:
    path = os.path.join(root, "check_docstrings_fixtures.py")
    cpath = os.path.join(root, CANARY_CONTROL_FILE)
    for p in (path, cpath):
        if not os.path.exists(p):
            print(f"canary: fixture file missing: {p}")
            return 2
    findings = _audit_file(path)
    caught = {f.name for f in findings if f.verdict == VERDICT_CONTRADICTED}
    print("=" * 78)
    print("canary — the four real over-claiming docstrings, one per review round")
    print("=" * 78)
    ok = True
    for name, what in CANARY_MUST_CATCH.items():
        hit = name in caught
        ok &= hit
        print(f"  {'CAUGHT ' if hit else 'MISSED '}  {what}")
        print(f"            {name}")
        if hit:
            for f in findings:
                if f.name == name and f.verdict == VERDICT_CONTRADICTED:
                    print(f"            -> {f.rule}: {f.detail.splitlines()[0][:120]}")
                    break
    drift_ok = _canary_interface_drift()
    ok &= drift_ok
    print(f"  {'CAUGHT ' if drift_ok else 'MISSED '}  a frozen-interface summary "
          f"the source docstring no longer says")
    print("            (round 4's 'the ONLY exit' survived in "
          "ESTATES_DB_INTERFACE.md after the docstring was fixed)")

    print(f"\n  controls ({CANARY_CONTROL_FILE}) — the same words, true of the "
          f"code beside them:")
    control_bad = [f for f in _audit_file(cpath)
                   if f.verdict == VERDICT_CONTRADICTED]
    if control_bad:
        ok = False
        for f in control_bad:
            print(f"  FALSE+   {f.name} ({f.rule}): {f.detail[:100]}")
    else:
        print("  ok       no control docstring was flagged")
    unexpected = caught - set(CANARY_MUST_CATCH)
    if unexpected:
        ok = False
        print("\n  unexpected CONTRADICTED in the fixture file:")
        for u in sorted(unexpected):
            print(f"    {u}")
    print("\n" + "=" * 78)
    print("canary: PASS — the tool still catches what it was written for"
          if ok else
          "canary: FAIL — do not trust a clean run from this build")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--all", action="store_true",
                    help="list UNVERIFIABLE and CONSISTENT findings in full")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true",
                    help="CONTRADICTED findings only, no summary")
    ap.add_argument("--canary", action="store_true",
                    help="self-test against the four historical defects")
    ap.add_argument("--modules", nargs="*", default=None,
                    help="override the audited module list")
    args = ap.parse_args(argv)

    try:
        if args.canary:
            return canary(args.root)
        modules = args.modules if args.modules is not None else MODULES
        funcs, idx = load(args.root, modules, PEER_ONLY)
        findings, stats = audit(funcs, idx)
        findings = interface_drift(args.root, funcs) + findings
        findings.sort(key=lambda h: (_SEVERITY[h.verdict], h.name))
    except ToolError as e:
        print(f"check_docstrings: cannot run: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"stats": stats,
                          "findings": [asdict(f) for f in findings]}, indent=2))
        return 1 if any(f.verdict == VERDICT_CONTRADICTED for f in findings) else 0
    return report(findings, stats, args.all, args.quiet)


if __name__ == "__main__":
    sys.exit(main())
