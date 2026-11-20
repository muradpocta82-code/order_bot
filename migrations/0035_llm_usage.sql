-- 0035: расход ИИ по дням (E4). Видно, сколько вызовов и во сколько это обходится.

CREATE TABLE IF NOT EXISTS llm_usage (
    day date NOT NULL,
    provider text NOT NULL,
    calls integer NOT NULL DEFAULT 0,
    tokens integer NOT NULL DEFAULT 0,
    est_cost numeric(12,4) NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (day, provider)
);
