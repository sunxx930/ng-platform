-- 死表清理（2026-09-01，P1-3）：事件溯源取代，7 表 0 行 0 引用。
-- CASCADE 处理 messages→sessions FK 顺序 + 001 GRANT 引用；IF EXISTS 幂等
-- （全新 docker init 走 schema.sql 已无这些表，迁移仍跑，IF EXISTS 兜底）。
DROP TABLE IF EXISTS deliveries, handovers, approvals, reviews, deliverables,
    messages, sessions CASCADE;
