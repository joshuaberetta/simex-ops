"""Placeholder roster for a fresh database: four teams, two people each, all on the
reserved .invalid domain so a live send can never reach them. Replace on the People tab."""

from .db import Person, Team, audit


def seed(s):
    if s.query(Team).count():
        return
    for n, letter in enumerate("ABCD"):
        t = Team(code=letter.lower(), name=f"Team {letter}", sort=n)
        s.add(t)
        s.flush()
        s.add(Person(name=f"Team {letter} Leader (placeholder)", email=f"team-{letter.lower()}-tl@placeholder.invalid",
                     team_id=t.id, role="tl"))
        s.add(Person(name=f"Team {letter} Member (placeholder)", email=f"team-{letter.lower()}-1@placeholder.invalid",
                     team_id=t.id))
    audit(s, "seed", "placeholder teams A-D")
    s.commit()
