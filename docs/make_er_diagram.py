"""
make_er_diagram.py - draws the ER diagram from the LIVE MySQL schema.

It reads information_schema (tables, columns, keys, foreign keys), so the
diagram always matches schema.sql exactly. Needs Graphviz (`dot`) installed.

    python docs/make_er_diagram.py        -> docs/er_diagram.png and docs/er_diagram.svg
"""
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
import db      # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ORDER = ["hospitals", "resources", "doctors", "users", "sos_requests", "allocations", "audit_log",
         "request_status_history"]
NOTE = {
    "hospitals": "hospital", "resources": "shared resource (OS)", "doctors": "specialist",
    "users": "login + role", "sos_requests": "process (OS)", "allocations": "booking (ACID tx)",
    "audit_log": "event history", "request_status_history": "written by triggers",
}


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def short_type(t):
    t = t.replace("tinyint(1)", "bool")
    if t.startswith("enum("):
        return "enum"
    return t


def main():
    cols = db.query_all(
        "SELECT table_name AS t, column_name AS c, column_type AS ty, column_key AS k, is_nullable AS n "
        "FROM information_schema.columns WHERE table_schema = %s ORDER BY table_name, ordinal_position",
        (config.DB_NAME,))
    fks = db.query_all(
        "SELECT k.table_name AS t, k.column_name AS c, k.referenced_table_name AS rt, "
        "k.referenced_column_name AS rc, r.delete_rule AS rule "
        "FROM information_schema.key_column_usage k JOIN information_schema.referential_constraints r "
        "ON r.constraint_name = k.constraint_name AND r.constraint_schema = k.table_schema "
        "WHERE k.table_schema = %s AND k.referenced_table_name IS NOT NULL", (config.DB_NAME,))
    fk_cols = {(f["t"], f["c"]) for f in fks}
    tables = {}
    for c in cols:
        tables.setdefault(c["t"], []).append(c)
    if not tables:
        sys.exit(f"No tables in database '{config.DB_NAME}'. Run python init_db.py first.")

    out = ['digraph ER {', '  graph [rankdir=LR, fontname="Helvetica", nodesep=0.5, ranksep=1.1, pad=0.3,',
           '         label="EmergencySOS - ER diagram (MySQL 8, InnoDB)  |  PK = primary key, FK = foreign key, UQ = unique",',
           '         labelloc=t, fontsize=18];',
           '  node [shape=plaintext, fontname="Helvetica", fontsize=11];',
           '  edge [fontname="Helvetica", fontsize=9, color="#475569", arrowhead=crow, arrowtail=tee, dir=both];']
    for t in [x for x in ORDER if x in tables] + [x for x in tables if x not in ORDER]:
        rows = []
        for c in tables[t]:
            tags = []
            if c["k"] == "PRI":
                tags.append("PK")
            if (t, c["c"]) in fk_cols:
                tags.append("FK")
            if c["k"] == "UNI":
                tags.append("UQ")
            key = " ".join(tags)
            bold = c["k"] == "PRI"
            name = f"<b>{esc(c['c'])}</b>" if bold else esc(c["c"])
            null = "" if c["n"] == "NO" else " ?"
            key_cell = f'<font color="#b91c1c">{key}</font>' if key else " "
            rows.append(f'<tr><td align="left" port="{c["c"]}">{key_cell}</td>'
                        f'<td align="left">{name}</td><td align="left"><font color="#64748b">'
                        f'{esc(short_type(c["ty"]))}{null}</font></td></tr>')
        head = (f'<tr><td colspan="3" bgcolor="#1565c0"><font color="white"><b>{t}</b></font>'
                f'<br/><font color="#dbeafe" point-size="9">{NOTE.get(t, "")}</font></td></tr>')
        out.append(f'  {t} [label=<<table border="0" cellborder="1" cellspacing="0" cellpadding="4" '
                   f'color="#cbd5e1">{head}{"".join(rows)}</table>>];')
    for f in fks:
        out.append(f'  {f["rt"]}:{f["rc"]} -> {f["t"]}:{f["c"]} '
                   f'[label="1 : N\\non delete {f["rule"].lower()}"];')
    out.append("}")

    dot_path = os.path.join(HERE, "er_diagram.dot")
    with open(dot_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    if not shutil.which("dot"):
        sys.exit(f"Wrote {dot_path}. Install Graphviz to render it, or paste it into an online Graphviz viewer.")
    for fmt in ("png", "svg"):
        subprocess.run(["dot", f"-T{fmt}", "-Gdpi=130" if fmt == "png" else "-Gdpi=72", dot_path,
                        "-o", os.path.join(HERE, f"er_diagram.{fmt}")], check=True)
    print(f"Written docs/er_diagram.png, .svg and .dot ({len(tables)} tables, {len(fks)} foreign keys)")


if __name__ == "__main__":
    main()
