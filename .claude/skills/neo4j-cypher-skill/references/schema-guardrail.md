# Schema Guardrail Reference

The schema comes from the live server: run the schema queries in `SKILL.md` ("Schema-First Protocol"), or use a schema already in context. Upstream also describes a `<db-name>-schema.json` file built by three helper scripts. That workflow is not part of this copy.

---

## Validation Rules

Reason about intent before asking. Ask only when unable to resolve — never generate wrong Cypher silently, but don't stop when a safe interpretation exists.

**1. Existence** — labels, rel-types, properties must be in schema. On miss: try synonym resolution → structural match → ask.

**2. Synonym mapping**
- Unambiguous → resolve silently: `ℹ️ Resolved 'Minifigure' → 'Minifig'.`
- Ambiguous → pick most likely from context, note: `ℹ️ 'Fig' → 'Minifig' (context). Correct if wrong.`
- No match → surface candidates: `⚠️ 'Character' not found. Did you mean: Theme, Set, Minifig?`

**3. Property type** — valid types: `STRING` `INTEGER` `FLOAT` `BOOLEAN` `DATE` `DATETIME` `LOCAL_DATETIME` `TIME` `LOCAL_TIME` `DURATION` `POINT` `LIST<TYPE>`. On mismatch:
- String against INTEGER (`'unknown'`, `'n/a'`) → rewrite as `IS NULL` and note
- Clearly wrong literal → propose correction and ask

**4. Relationship direction** — `out` = `(a)-[:R]->(b)`, `in` = `(b)-[:R]->(a)`. Wrong direction → correct silently, note:
```
HAS_SET | Schema: Theme──→Set | Prompt: Set──→Theme | ↩ Corrected
```

**5. Generate** — Cypher 5; use literals for interactive execution, `$param` for code generation; return only schema-declared properties.

---

## Examples

The schema in these examples is a toy: nodes `Theme`, `Set`, `Minifig`.

### Valid query
```
User: "List minifigures in the Cloud City set"

✅ Set | ✅ Minifig | ✅ HAS_MINIFIG (Set→Minifig)

MATCH (s:Set {id: $setId})-[:HAS_MINIFIG]->(m:Minifig)
RETURN m.name AS minifigName, m.fig_num AS figNum, m.num_parts AS numParts
ORDER BY m.name
// Parameters: { setId: "10123-1" }
```

### Entity not found
```
User: "Find all Character nodes linked to a Movie"

❌ Character NOT FOUND | ❌ Movie NOT FOUND
Schema nodes: Theme, Set, Minifig

⚠️ Neither 'Character' nor 'Movie' exists in this schema.
Did you mean Set linked to Minifig, or are you querying a different database?
```

### Synonym resolved
```
User: "Find all Minifigures in a set"

ℹ️ Resolved 'Minifigure' → 'Minifig'. Proceeding.

MATCH (s:Set {id: $setId})-[:HAS_MINIFIG]->(m:Minifig)
RETURN m.name AS minifigName, m.fig_num AS figNum
// Parameters: { setId: $setId }
```

### Type mismatch
```
User: "Find sets where pieces is 'unknown'"

Set.pieces declared INTEGER, value 'unknown' is a STRING.
Interpreting as null/missing-value check.

ℹ️ Rewritten: WHERE s.pieces IS NULL. Correct if you meant something else.

MATCH (s:Set) WHERE s.pieces IS NULL RETURN s.name, s.id
```
