-- ARC 16 — live RLS isolation proof against real Postgres.
-- Seeds two tenants (admin A + admin B), each with an instance, a source,
-- and chunks, then connects as the non-superuser app role (rls_tester,
-- NOBYPASSRLS) and proves that with app.admin_id = A, tenant B's rows are
-- invisible — and with app.admin_id unset, EVERYTHING is invisible
-- (fail-closed). This is the Wall-1 / Wall-3 guarantee, executed not asserted.

\set ON_ERROR_STOP on

-- ---- Seed as superuser (bypasses RLS for setup) --------------------------
INSERT INTO admins (id, name) VALUES ('admin_A', 'Tenant A') ON CONFLICT (id) DO NOTHING;
INSERT INTO admins (id, name) VALUES ('admin_B', 'Tenant B') ON CONFLICT (id) DO NOTHING;

INSERT INTO instances (admin_id, instance_slug, display_name)
VALUES ('admin_A', 'a-inst', 'A Inst') ON CONFLICT DO NOTHING;
INSERT INTO instances (admin_id, instance_slug, display_name)
VALUES ('admin_B', 'b-inst', 'B Inst') ON CONFLICT DO NOTHING;

-- capture instance ids
\gset
SELECT id AS a_inst FROM instances WHERE admin_id='admin_A' AND instance_slug='a-inst' \gset
SELECT id AS b_inst FROM instances WHERE admin_id='admin_B' AND instance_slug='b-inst' \gset

INSERT INTO knowledge_sources (admin_id, luciel_instance_id, source_type, size_bytes, ingested_by, ingestion_status)
VALUES ('admin_A', :a_inst, 'txt', 10, 'seed', 'ready')
RETURNING id AS a_src \gset
INSERT INTO knowledge_sources (admin_id, luciel_instance_id, source_type, size_bytes, ingested_by, ingestion_status)
VALUES ('admin_B', :b_inst, 'txt', 10, 'seed', 'ready')
RETURNING id AS b_src \gset

INSERT INTO knowledge_chunks (admin_id, luciel_instance_id, content, knowledge_type, source_id)
VALUES ('admin_A', :a_inst, 'SECRET_A_ONE', 'tenant_document', :a_src),
       ('admin_A', :a_inst, 'SECRET_A_TWO', 'tenant_document', :a_src);
INSERT INTO knowledge_chunks (admin_id, luciel_instance_id, content, knowledge_type, source_id)
VALUES ('admin_B', :b_inst, 'SECRET_B_ONE', 'tenant_document', :b_src);

GRANT ALL ON ALL TABLES IN SCHEMA public TO rls_tester;

-- ---- Now act as the non-superuser app role -------------------------------
SET ROLE rls_tester;

\echo '--- TEST 1: app.admin_id UNSET => fail-closed => expect 0 rows ---'
SELECT count(*) AS rows_visible_when_unset FROM knowledge_chunks;

\echo '--- TEST 2: app.admin_id = admin_A => expect ONLY A rows (2) ---'
SET app.admin_id = 'admin_A';
SELECT count(*) AS a_rows_visible, string_agg(content, ',' ORDER BY content) AS contents FROM knowledge_chunks;

\echo '--- TEST 3: as admin_A, can I see ANY of B''s content? expect 0 ---'
SELECT count(*) AS b_rows_leaked_to_a FROM knowledge_chunks WHERE content LIKE 'SECRET_B%';

\echo '--- TEST 4: switch to admin_B => expect ONLY B rows (1) ---'
SET app.admin_id = 'admin_B';
SELECT count(*) AS b_rows_visible, string_agg(content, ',' ORDER BY content) AS contents FROM knowledge_chunks;

\echo '--- TEST 5: knowledge_sources isolation under admin_B => expect 1 (B only) ---'
SELECT count(*) AS b_sources_visible FROM knowledge_sources;

RESET ROLE;
