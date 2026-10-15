"""Migration 0007: relational performer credits, JSONB lists, and integrity."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    # Tolerate historical malformed values rather than aborting the migration.
    conn.execute("""
        CREATE OR REPLACE FUNCTION adult_hub_is_text_array(value jsonb)
        RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
            SELECT CASE WHEN jsonb_typeof(value) IS DISTINCT FROM 'array' THEN false
                ELSE NOT EXISTS (
                    SELECT 1 FROM jsonb_array_elements(value) item
                    WHERE jsonb_typeof(item) <> 'string'
                ) END
        $$
    """)

    conn.execute("""
        CREATE OR REPLACE FUNCTION adult_hub_text_array_valid(value text, split_legacy boolean DEFAULT false)
        RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $$
        DECLARE parsed jsonb;
        BEGIN
            IF value IS NULL OR btrim(value) = '' THEN RETURN true; END IF;
            BEGIN
                parsed := value::jsonb;
            EXCEPTION WHEN others THEN
                RETURN split_legacy AND left(btrim(value), 1) <> '[';
            END;
            IF jsonb_typeof(parsed) <> 'array' THEN RETURN false; END IF;
            RETURN NOT EXISTS (
                SELECT 1 FROM jsonb_array_elements(parsed) item
                WHERE jsonb_typeof(item) <> 'string'
            );
        END $$
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_metadata_migration_issues (
            video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            field_name TEXT NOT NULL,
            original_value TEXT NOT NULL,
            PRIMARY KEY (video_id, field_name)
        )
    """)
    for column in ("performers", "categories", "tags"):
        split_legacy = column == "categories"
        conn.execute(
            f"INSERT INTO video_metadata_migration_issues (video_id, field_name, original_value) "
            f"SELECT id, %s, {column} FROM videos WHERE NOT adult_hub_text_array_valid({column}, %s) "
            "ON CONFLICT (video_id, field_name) DO NOTHING",
            (column, split_legacy),
        )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS performer_bio_migration_issues (
            name TEXT PRIMARY KEY,
            original_aliases TEXT NOT NULL
        )
    """)
    conn.execute("""
        INSERT INTO performer_bio_migration_issues (name, original_aliases)
        SELECT name, aliases FROM performer_bios
        WHERE NOT adult_hub_text_array_valid(aliases)
        ON CONFLICT (name) DO NOTHING
    """)

    conn.execute("""
        CREATE OR REPLACE FUNCTION adult_hub_text_array(value text, split_legacy boolean DEFAULT false)
        RETURNS jsonb LANGUAGE plpgsql IMMUTABLE AS $$
        DECLARE parsed jsonb;
        BEGIN
            IF value IS NULL THEN RETURN NULL; END IF;
            IF btrim(value) = '' THEN RETURN '[]'::jsonb; END IF;
            BEGIN
                parsed := value::jsonb;
            EXCEPTION WHEN others THEN
                IF split_legacy AND left(btrim(value), 1) <> '[' THEN
                    RETURN to_jsonb(string_to_array(value, ','));
                END IF;
                RETURN '[]'::jsonb;
            END;
            IF jsonb_typeof(parsed) <> 'array' THEN RETURN '[]'::jsonb; END IF;
            RETURN COALESCE((
                SELECT jsonb_agg(elem ORDER BY ordinality)
                FROM jsonb_array_elements(parsed) WITH ORDINALITY AS values(elem, ordinality)
                WHERE jsonb_typeof(elem) = 'string'
            ), '[]'::jsonb);
        END $$
    """)

    for column in ("performers", "categories", "tags"):
        conn.execute(
            f"ALTER TABLE videos ALTER COLUMN {column} TYPE JSONB "
            f"USING adult_hub_text_array({column}{', true' if column == 'categories' else ''})"
        )
        conn.execute(
            f"ALTER TABLE videos ADD CONSTRAINT videos_{column}_array_ck "
            f"CHECK ({column} IS NULL OR adult_hub_is_text_array({column}))"
        )
        if column != "performers":
            conn.execute(f"UPDATE videos SET {column} = '[]'::jsonb WHERE {column} IS NULL")
            conn.execute(f"ALTER TABLE videos ALTER COLUMN {column} SET DEFAULT '[]'::jsonb")
            conn.execute(f"ALTER TABLE videos ALTER COLUMN {column} SET NOT NULL")

    conn.execute("ALTER TABLE performer_bios ALTER COLUMN aliases DROP DEFAULT")
    conn.execute("""
        ALTER TABLE performer_bios ALTER COLUMN aliases TYPE JSONB
        USING adult_hub_text_array(aliases)
    """)
    conn.execute("""
        ALTER TABLE performer_bios ADD CONSTRAINT performer_bios_aliases_array_ck
        CHECK (adult_hub_is_text_array(aliases))
    """)
    conn.execute("UPDATE performer_bios SET aliases = '[]'::jsonb WHERE aliases IS NULL")
    conn.execute("ALTER TABLE performer_bios ALTER COLUMN aliases SET DEFAULT '[]'::jsonb")
    conn.execute("ALTER TABLE performer_bios ALTER COLUMN aliases SET NOT NULL")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_performers (
            video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            performer_name TEXT NOT NULL CHECK (btrim(performer_name) <> ''),
            position INTEGER NOT NULL CHECK (position >= 0),
            PRIMARY KEY (video_id, performer_name),
            UNIQUE (video_id, position)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_video_performers_name ON video_performers (performer_name, video_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_performer_migration_issues (
            video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            performer_key TEXT NOT NULL,
            occurrences INTEGER NOT NULL CHECK (occurrences > 1),
            source_names JSONB NOT NULL CHECK (adult_hub_is_text_array(source_names)),
            source_positions INTEGER[] NOT NULL,
            PRIMARY KEY (video_id, performer_key)
        )
    """)
    conn.execute("""
        INSERT INTO video_performer_migration_issues
            (video_id, performer_key, occurrences, source_names, source_positions)
        SELECT v.id, lower(btrim(item.value)), count(*)::integer,
               jsonb_agg(to_jsonb(item.value) ORDER BY item.ordinality),
               array_agg((item.ordinality - 1)::integer ORDER BY item.ordinality)
        FROM videos v
        CROSS JOIN LATERAL jsonb_array_elements_text(v.performers)
            WITH ORDINALITY AS item(value, ordinality)
        WHERE btrim(item.value) <> ''
        GROUP BY v.id, lower(btrim(item.value))
        HAVING count(*) > 1
        ON CONFLICT (video_id, performer_key) DO NOTHING
    """)

    # Keep older application flows working during the staged code transition.
    conn.execute("""
        CREATE OR REPLACE FUNCTION adult_hub_sync_video_performers()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            DELETE FROM video_performers WHERE video_id = NEW.id;
            INSERT INTO video_performers (video_id, performer_name, position)
            SELECT NEW.id, performer_name,
                   (row_number() OVER (ORDER BY first_position) - 1)::integer
            FROM (
                SELECT elem #>> '{}' AS performer_name, min(ordinality) AS first_position
            FROM jsonb_array_elements(COALESCE(NEW.performers, '[]'::jsonb))
                    WITH ORDINALITY AS item(elem, ordinality)
                WHERE jsonb_typeof(elem) = 'string' AND btrim(elem #>> '{}') <> ''
                GROUP BY elem #>> '{}'
            ) credits
            ORDER BY first_position;
            RETURN NEW;
        END $$
    """)
    conn.execute("""
        DROP TRIGGER IF EXISTS videos_sync_video_performers ON videos
    """)
    conn.execute("""
        CREATE TRIGGER videos_sync_video_performers
        AFTER INSERT OR UPDATE OF performers ON videos
        FOR EACH ROW EXECUTE FUNCTION adult_hub_sync_video_performers()
    """)

    # Backfill ordered credits from the original JSON lists.
    conn.execute("""
        INSERT INTO video_performers (video_id, performer_name, position)
        SELECT v.id, credits.performer_name,
               (row_number() OVER (PARTITION BY v.id ORDER BY credits.first_position) - 1)::integer
        FROM videos v
        CROSS JOIN LATERAL (
            SELECT item.value AS performer_name, min(item.ordinality) AS first_position
            FROM jsonb_array_elements_text(v.performers)
                WITH ORDINALITY AS item(value, ordinality)
            WHERE btrim(item.value) <> ''
            GROUP BY item.value
        ) credits
    """)

    # Enforce referential integrity on new writes immediately; validate legacy
    # rows only when the current data is clean.
    _add_fk_if_missing(conn, "video_categories", "video_id", "videos", "id", "video_categories_video_fk")
    conn.execute("DROP FUNCTION adult_hub_text_array(text, boolean)")
    conn.execute("DROP FUNCTION adult_hub_text_array_valid(text, boolean)")


def _add_fk_if_missing(conn: Any, table: str, column: str, ref_table: str, ref_column: str, name: str) -> None:
    exists = conn.execute(
        "SELECT 1 FROM pg_constraint WHERE conname = %s AND conrelid = %s::regclass",
        (name, table),
    ).fetchone()
    if exists:
        return
    conn.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY ({column}) REFERENCES {ref_table}({ref_column}) ON DELETE CASCADE NOT VALID")
    orphan = conn.execute(
        f"SELECT 1 FROM {table} child LEFT JOIN {ref_table} parent "
        f"ON child.{column} = parent.{ref_column} "
        f"WHERE child.{column} IS NOT NULL AND parent.{ref_column} IS NULL LIMIT 1"
    ).fetchone()
    if not orphan:
        conn.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")


migration = Migration(
    version=7,
    name="0007_relational_metadata",
    description="Normalize performer credits, store list documents as JSONB, and add safe foreign keys",
    apply_fn=apply,
)
