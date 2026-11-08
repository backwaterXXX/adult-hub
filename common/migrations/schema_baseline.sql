-- Canonical baseline schema for Adult Hub (squashed from the historical
-- chain: legacy v1-v8 plus post-baseline v10-v49, and the end state of the
-- forward upgrades v50-v59 that CURRENT_MIGRATIONS replay onto a fresh base).
--
-- Applied by common/migrations/m0001_baseline.py to a fresh database; existing
-- databases never re-run it. Generated with:
--   pg_dump --schema-only --no-owner --no-privileges --no-tablespaces
-- from a database built by the full historical chain, then stripped of the
-- pg_dump preamble and the schema_migrations bookkeeping table.
-- tests/test_migrations.py asserts this script produces the same schema as that
-- chain, so edit both together when adding a migration.
--
-- One deliberate deviation from raw pg_dump output: functions are declared
-- CREATE OR REPLACE, because recreate_database() drops tables and their
-- triggers but not schema-level functions, and it must stay re-runnable.

CREATE OR REPLACE FUNCTION public.adult_hub_is_text_array(value jsonb) RETURNS boolean
    LANGUAGE sql IMMUTABLE
    AS $$
            SELECT CASE WHEN jsonb_typeof(value) IS DISTINCT FROM 'array' THEN false
                ELSE NOT EXISTS (
                    SELECT 1 FROM jsonb_array_elements(value) item
                    WHERE jsonb_typeof(item) <> 'string'
                ) END
        $$;

CREATE OR REPLACE FUNCTION public.adult_hub_sync_video_performers() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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
        END $$;

CREATE OR REPLACE FUNCTION public.archive_video_visual_row() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
      IF TG_TABLE_NAME = 'video_visual_signatures' THEN
        INSERT INTO video_visual_history(kind,record,image)
          VALUES ('signature', to_jsonb(OLD) - 'contact_sheet', OLD.contact_sheet);
      ELSE
        INSERT INTO video_visual_history(kind,record,image)
          VALUES ('comparison', to_jsonb(OLD) - 'frame_sheet', OLD.frame_sheet);
      END IF;
      IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END; $$;

CREATE OR REPLACE FUNCTION public.reject_unverified_short_video_duration() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.duration IS NULL OR NEW.duration < 420 THEN
                    RAISE EXCEPTION 'video duration must be known and at least 420 seconds'
                        USING ERRCODE = 'check_violation';
                END IF;
            ELSIF NEW.duration IS DISTINCT FROM OLD.duration THEN
                IF NEW.duration IS NULL OR NEW.duration < 420 THEN
                    RAISE EXCEPTION 'video duration must be known and at least 420 seconds'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;

CREATE TABLE public.admin_action_events (
    id bigint NOT NULL,
    actor_type text DEFAULT 'shared_admin'::text NOT NULL,
    method text NOT NULL,
    route text NOT NULL,
    status_code integer NOT NULL,
    outcome text NOT NULL,
    correlation_id text,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT admin_action_events_actor_type_check CHECK ((actor_type = 'shared_admin'::text)),
    CONSTRAINT admin_action_events_correlation_id_check CHECK (((correlation_id IS NULL) OR (correlation_id ~ '^[a-f0-9]{32}$'::text))),
    CONSTRAINT admin_action_events_method_check CHECK ((method = ANY (ARRAY['POST'::text, 'PUT'::text, 'PATCH'::text, 'DELETE'::text]))),
    CONSTRAINT admin_action_events_outcome_check CHECK ((outcome = ANY (ARRAY['completed'::text, 'accepted'::text]))),
    CONSTRAINT admin_action_events_route_check CHECK (((length(route) >= 1) AND (length(route) <= 256))),
    CONSTRAINT admin_action_events_status_code_check CHECK (((status_code >= 100) AND (status_code <= 599)))
);

CREATE SEQUENCE public.admin_action_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.admin_action_events_id_seq OWNED BY public.admin_action_events.id;

CREATE TABLE public.admin_audit_events (
    id bigint NOT NULL,
    event_key text NOT NULL,
    actor_type text DEFAULT 'shared_admin'::text NOT NULL,
    event_type text NOT NULL,
    object_type text NOT NULL,
    object_id text NOT NULL,
    job_id text,
    changes jsonb NOT NULL,
    truncated boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT admin_audit_events_actor_type_check CHECK ((actor_type = 'shared_admin'::text)),
    CONSTRAINT admin_audit_events_changes_check CHECK ((jsonb_typeof(changes) = 'object'::text)),
    CONSTRAINT admin_audit_events_event_type_check CHECK ((event_type = ANY (ARRAY['metadata_update'::text, 'source_settings_update'::text, 'admin_operation_queued'::text, 'schedule_created'::text, 'schedule_enabled'::text, 'performer_profile_update'::text]))),
    CONSTRAINT admin_audit_events_object_type_check CHECK ((object_type = ANY (ARRAY['video'::text, 'source_configuration'::text, 'operation_plan'::text, 'schedule'::text, 'performer'::text])))
);

CREATE SEQUENCE public.admin_audit_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.admin_audit_events_id_seq OWNED BY public.admin_audit_events.id;

CREATE TABLE public.admin_audit_items (
    id bigint NOT NULL,
    job_id text NOT NULL,
    video_id text,
    kind text NOT NULL,
    status text NOT NULL,
    evidence text DEFAULT ''::text NOT NULL,
    checked_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT admin_audit_items_kind_check CHECK ((kind = ANY (ARRAY['missing_stream'::text, 'missing_nfo'::text, 'invalid_nfo'::text, 'nfo_metadata_mismatch'::text, 'catalog_reference_without_video'::text, 'unknown_mapping'::text, 'orphan_file'::text, 'nfo_import_state_missing'::text, 'nfo_import_state_mismatch'::text, 'missing_artwork'::text, 'missing_metadata'::text]))),
    CONSTRAINT admin_audit_items_status_check CHECK ((status = ANY (ARRAY['open'::text, 'resolved'::text])))
);

ALTER TABLE public.admin_audit_items ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.admin_audit_items_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.admin_operation_plans (
    plan_id text NOT NULL,
    operation_type text NOT NULL,
    state text NOT NULL,
    scope jsonb NOT NULL,
    revisions jsonb NOT NULL,
    preview jsonb NOT NULL,
    result jsonb,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    CONSTRAINT admin_operation_plans_state_check CHECK ((state = ANY (ARRAY['previewed'::text, 'queued'::text, 'expired'::text])))
);

CREATE TABLE public.admin_orphan_quarantine (
    operation_id text NOT NULL,
    audit_id text NOT NULL,
    job_id text,
    state text NOT NULL,
    files jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT admin_orphan_quarantine_state_check CHECK ((state = ANY (ARRAY['moving'::text, 'active'::text, 'restoring'::text, 'restored'::text, 'failed'::text])))
);

CREATE TABLE public.admin_quality_acknowledgements (
    id bigint NOT NULL,
    video_id text NOT NULL,
    kind text NOT NULL,
    finding_fingerprint text NOT NULL,
    source_audit_id text NOT NULL,
    source_finding_id bigint NOT NULL,
    note text NOT NULL,
    state text DEFAULT 'active'::text NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT admin_quality_acknowledgements_finding_fingerprint_check CHECK ((finding_fingerprint ~ '^[0-9a-f]{32}$'::text)),
    CONSTRAINT admin_quality_acknowledgements_kind_check CHECK ((kind = ANY (ARRAY['missing_metadata'::text, 'missing_nfo'::text, 'invalid_nfo'::text, 'nfo_metadata_mismatch'::text, 'missing_artwork'::text]))),
    CONSTRAINT admin_quality_acknowledgements_note_check CHECK (((length(note) >= 1) AND (length(note) <= 500))),
    CONSTRAINT admin_quality_acknowledgements_state_check CHECK ((state = ANY (ARRAY['active'::text, 'reopened'::text])))
);

ALTER TABLE public.admin_quality_acknowledgements ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.admin_quality_acknowledgements_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.admin_schedules (
    id text NOT NULL,
    name text NOT NULL,
    action text NOT NULL,
    payload text DEFAULT '{}'::text NOT NULL,
    schedule_type text NOT NULL,
    interval_minutes integer,
    daily_at text,
    cron_expression text,
    timezone text DEFAULT 'Europe/Warsaw'::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    revision integer DEFAULT 1 NOT NULL,
    next_run_at timestamp with time zone,
    last_run_at timestamp with time zone,
    last_job_id text,
    last_result text,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT admin_schedules_action_check CHECK ((action = ANY (ARRAY['availability_check'::text, 'consistency_audit'::text, 'metadata_quality_audit'::text, 'catalog_full_audit'::text, 'backup_create'::text, 'duplicate_reconcile'::text]))),
    CONSTRAINT admin_schedules_check CHECK ((((schedule_type = 'interval'::text) AND (interval_minutes IS NOT NULL) AND (interval_minutes >= 15) AND (interval_minutes <= 43200) AND (daily_at IS NULL) AND (cron_expression IS NULL)) OR ((schedule_type = 'daily'::text) AND (interval_minutes IS NULL) AND (daily_at IS NOT NULL) AND (daily_at ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'::text) AND (cron_expression IS NULL)) OR ((schedule_type = 'cron'::text) AND (interval_minutes IS NULL) AND (daily_at IS NULL) AND (cron_expression IS NOT NULL) AND (length(cron_expression) >= 1) AND (length(cron_expression) <= 100)))),
    CONSTRAINT admin_schedules_schedule_type_check CHECK ((schedule_type = ANY (ARRAY['interval'::text, 'daily'::text, 'cron'::text]))),
    CONSTRAINT admin_schedules_timezone_check CHECK ((timezone = 'Europe/Warsaw'::text))
);

CREATE TABLE public.system_schedule_stages (
    schedule_id text NOT NULL,
    id text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    revision integer DEFAULT 1 NOT NULL,
    last_result text,
    last_error text,
    last_run_at timestamp with time zone,
    last_finished_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT system_schedule_stages_last_result_check CHECK ((last_result = ANY (ARRAY['queued'::text, 'running'::text, 'done'::text, 'failed'::text]))),
    CONSTRAINT system_schedule_stages_revision_check CHECK ((revision >= 1))
);

CREATE TABLE public.system_schedules (
    id text NOT NULL,
    name text NOT NULL,
    cron_expression text,
    legacy_interval_minutes integer,
    legacy_daily_at text,
    needs_conversion boolean DEFAULT false NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    revision integer DEFAULT 1 NOT NULL,
    next_run_at timestamp with time zone,
    last_run_at timestamp with time zone,
    last_finished_at timestamp with time zone,
    last_job_id text,
    last_result text,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT system_schedules_id_check CHECK ((id = ANY (ARRAY['library-sync'::text, 'database-backup'::text, 'artwork-generation'::text, 'scene-fingerprints'::text]))),
    CONSTRAINT system_schedules_revision_check CHECK ((revision >= 1)),
    CONSTRAINT system_schedules_check CHECK ((((cron_expression IS NOT NULL) AND (length(cron_expression) >= 1) AND (length(cron_expression) <= 100) AND (legacy_interval_minutes IS NULL) AND (legacy_daily_at IS NULL) AND (needs_conversion = false)) OR ((cron_expression IS NULL) AND (needs_conversion = true) AND (((legacy_interval_minutes IS NOT NULL) AND (legacy_interval_minutes >= 15) AND (legacy_interval_minutes <= 43200) AND (legacy_daily_at IS NULL)) OR ((legacy_interval_minutes IS NULL) AND (legacy_daily_at IS NOT NULL) AND (legacy_daily_at ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'::text))))))
);

CREATE TABLE public.dead_streams (
    video_id text NOT NULL,
    reason text,
    detected_at timestamp with time zone
);

CREATE TABLE public.diagnostic_reports (
    report_id text NOT NULL,
    ended_at timestamp with time zone NOT NULL,
    report_json text NOT NULL
);

CREATE TABLE public.diagnostic_settings (
    key text NOT NULL,
    value text NOT NULL
);

CREATE TABLE public.external_metadata_attempts (
    entity_type text NOT NULL,
    entity_key text NOT NULL,
    source text NOT NULL,
    outcome text NOT NULL,
    attempt_count integer DEFAULT 1 NOT NULL,
    error_streak integer DEFAULT 0 NOT NULL,
    last_attempt_at timestamp with time zone NOT NULL,
    next_attempt_at timestamp with time zone NOT NULL,
    last_error_code text DEFAULT ''::text NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    last_error_detail text DEFAULT ''::text NOT NULL,
    CONSTRAINT external_metadata_attempts_attempt_count_check CHECK ((attempt_count > 0)),
    CONSTRAINT external_metadata_attempts_entity_type_check CHECK ((entity_type = ANY (ARRAY['video'::text, 'performer'::text]))),
    CONSTRAINT external_metadata_attempts_error_streak_check CHECK ((error_streak >= 0)),
    CONSTRAINT external_metadata_attempts_outcome_check CHECK ((outcome = ANY (ARRAY['found'::text, 'no_match'::text, 'error'::text])))
);

CREATE TABLE public.jellyfin_person_sync (
    name text NOT NULL,
    pbc_updated_at timestamp with time zone NOT NULL,
    format_version integer DEFAULT 0 NOT NULL
);

CREATE TABLE public.meta (
    key text NOT NULL,
    value text
);

CREATE TABLE public.metadata_review_proposals (
    id bigint NOT NULL,
    video_id text NOT NULL,
    candidate_fingerprint text NOT NULL,
    changes jsonb NOT NULL,
    source text DEFAULT ''::text NOT NULL,
    external_id text DEFAULT ''::text NOT NULL,
    source_url text DEFAULT ''::text NOT NULL,
    confidence double precision DEFAULT 0 NOT NULL,
    expected_revision integer DEFAULT 0 NOT NULL,
    expected_source_fingerprint text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    reviewed_at timestamp with time zone,
    attempt_count integer DEFAULT 0 NOT NULL,
    last_attempt_at timestamp with time zone,
    next_attempt_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    last_error text DEFAULT ''::text NOT NULL,
    CONSTRAINT metadata_review_proposals_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'accepted'::text, 'rejected'::text])))
);

CREATE SEQUENCE public.metadata_review_proposals_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.metadata_review_proposals_id_seq OWNED BY public.metadata_review_proposals.id;

CREATE TABLE public.performer_aliases (
    alias text NOT NULL,
    canonical text NOT NULL
);

CREATE TABLE public.performer_bio_migration_issues (
    name text NOT NULL,
    original_aliases text NOT NULL
);

CREATE TABLE public.performer_bios (
    name text NOT NULL,
    bio text DEFAULT ''::text,
    birth text DEFAULT ''::text,
    aliases jsonb DEFAULT '[]'::jsonb NOT NULL,
    updated_at timestamp with time zone,
    nationality text DEFAULT ''::text,
    stashdb_url text DEFAULT ''::text,
    pbc_url text DEFAULT ''::text,
    CONSTRAINT performer_bios_aliases_array_ck CHECK (public.adult_hub_is_text_array(aliases))
);

CREATE TABLE public.performer_catalog (
    id text NOT NULL,
    source text NOT NULL,
    source_id text NOT NULL,
    name text NOT NULL,
    updated_at timestamp with time zone,
    active boolean DEFAULT true NOT NULL
);

CREATE TABLE public.performer_catalog_aliases (
    id bigint NOT NULL,
    performer_id text NOT NULL,
    alias text NOT NULL,
    normalized text NOT NULL,
    origin text NOT NULL,
    approved boolean DEFAULT true NOT NULL
);

CREATE SEQUENCE public.performer_catalog_aliases_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.performer_catalog_aliases_id_seq OWNED BY public.performer_catalog_aliases.id;

CREATE TABLE public.performer_photos (
    name text NOT NULL,
    thumb text DEFAULT ''::text,
    updated_at timestamp with time zone
);

CREATE TABLE public.performer_profile_overrides (
    name text NOT NULL,
    fields jsonb DEFAULT '{}'::jsonb NOT NULL,
    revision integer DEFAULT 1 NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT performer_profile_overrides_fields_check CHECK ((jsonb_typeof(fields) = 'object'::text)),
    CONSTRAINT performer_profile_overrides_revision_check CHECK ((revision >= 1))
);

CREATE TABLE public.stream_cache (
    cache_key text NOT NULL,
    stream_url text NOT NULL,
    expire_at timestamp with time zone NOT NULL,
    last_accessed timestamp with time zone NOT NULL
);

CREATE TABLE public.synced_files (
    file_path text NOT NULL,
    video_id text,
    category text,
    created_at timestamp with time zone
);

CREATE TABLE public.telegram_channel_state (
    channel_id text NOT NULL,
    last_min_duration integer,
    backfill_in_progress integer DEFAULT 0,
    backfill_cursor bigint,
    backfill_completed_at timestamp with time zone,
    updated_at timestamp with time zone,
    last_tags_whitelist text,
    last_tags_blacklist text,
    latest_watermark bigint,
    latest_cursor bigint,
    latest_head bigint
);

CREATE TABLE public.telegram_performer_proposals (
    id bigint NOT NULL,
    video_id text NOT NULL,
    candidate text NOT NULL,
    normalized text NOT NULL,
    matched_text text DEFAULT ''::text NOT NULL,
    source_field text DEFAULT ''::text NOT NULL,
    reason text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    performer_id text,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT telegram_performer_proposals_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text])))
);

CREATE SEQUENCE public.telegram_performer_proposals_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.telegram_performer_proposals_id_seq OWNED BY public.telegram_performer_proposals.id;

CREATE TABLE public.telegram_performer_rebuild_backup (
    run_id uuid NOT NULL,
    video_id text NOT NULL,
    old_performers jsonb NOT NULL,
    new_performers jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE TABLE public.telegram_unavailable_quarantine (
    video_id text NOT NULL,
    channel_id text NOT NULL,
    message_id bigint NOT NULL,
    file_unique_id text NOT NULL,
    status text NOT NULL,
    miss_count integer DEFAULT 1 NOT NULL,
    last_checked_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    files jsonb DEFAULT '[]'::jsonb NOT NULL,
    last_error text DEFAULT ''::text NOT NULL,
    CONSTRAINT telegram_unavailable_quarantine_status_check CHECK ((status = ANY (ARRAY['suspect'::text, 'quarantined'::text, 'failed'::text])))
);

CREATE TABLE public.video_availability_checks (
    id bigint NOT NULL,
    job_id text NOT NULL,
    video_id text NOT NULL,
    status text NOT NULL,
    checked_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    evidence text DEFAULT ''::text NOT NULL,
    CONSTRAINT video_availability_checks_status_check CHECK ((status = ANY (ARRAY['available'::text, 'unavailable'::text, 'transient_error'::text, 'unknown'::text])))
);

ALTER TABLE public.video_availability_checks ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.video_availability_checks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.video_categories (
    video_id text NOT NULL,
    category text NOT NULL
);

CREATE TABLE public.video_duplicate_decisions (
    loser_id text NOT NULL,
    keeper_id text NOT NULL,
    operation_id text NOT NULL,
    state text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    snapshot text,
    rules_version integer,
    evidence text DEFAULT ''::text NOT NULL
);

CREATE TABLE public.video_duplicate_dismissals (
    left_id text NOT NULL,
    right_id text NOT NULL,
    active integer DEFAULT 1 NOT NULL,
    operation_id text NOT NULL,
    created_at timestamp with time zone NOT NULL
);

CREATE TABLE public.video_duplicate_jobs (
    id text NOT NULL,
    action text NOT NULL,
    state text NOT NULL,
    payload text NOT NULL,
    result text,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    lease_token text
);

CREATE TABLE public.video_fingerprints (
    video_id text NOT NULL,
    algorithm text NOT NULL,
    fingerprint text NOT NULL,
    computed_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    media_probe_status text,
    media_probe_bytes integer,
    media_probe_data jsonb,
    media_probe_updated_at timestamp with time zone,
    CONSTRAINT video_fingerprints_algorithm_check CHECK ((algorithm = ANY (ARRAY['OSHASH'::text, 'PHASH'::text]))),
    CONSTRAINT video_fingerprints_fingerprint_check CHECK ((fingerprint ~ '^[0-9a-f]{16}$'::text)),
    CONSTRAINT video_fingerprints_media_probe_bytes_check CHECK (((media_probe_bytes IS NULL) OR (media_probe_bytes >= 0))),
    CONSTRAINT video_fingerprints_media_probe_status_check CHECK (((media_probe_status IS NULL) OR (media_probe_status = ANY (ARRAY['complete'::text, 'partial'::text, 'unavailable'::text]))))
);

CREATE TABLE public.video_metadata_migration_issues (
    video_id text NOT NULL,
    field_name text NOT NULL,
    original_value text NOT NULL
);

CREATE TABLE public.video_metadata_overrides (
    video_id text NOT NULL,
    fields jsonb DEFAULT '{}'::jsonb NOT NULL,
    revision bigint DEFAULT 1 NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT video_metadata_overrides_revision_check CHECK ((revision > 0))
);

CREATE TABLE public.video_performer_migration_issues (
    video_id text NOT NULL,
    performer_key text NOT NULL,
    occurrences integer NOT NULL,
    source_names jsonb NOT NULL,
    source_positions integer[] NOT NULL,
    CONSTRAINT video_performer_migration_issues_occurrences_check CHECK ((occurrences > 1)),
    CONSTRAINT video_performer_migration_issues_source_names_check CHECK (public.adult_hub_is_text_array(source_names))
);

CREATE TABLE public.video_performers (
    video_id text NOT NULL,
    performer_name text NOT NULL,
    "position" integer NOT NULL,
    CONSTRAINT video_performers_performer_name_check CHECK ((btrim(performer_name) <> ''::text)),
    CONSTRAINT video_performers_position_check CHECK (("position" >= 0))
);

CREATE TABLE public.video_phash_jobs (
    id text NOT NULL,
    video_id text NOT NULL,
    algorithm_version text NOT NULL,
    decoder_profile text NOT NULL,
    duration_us bigint NOT NULL,
    submitted_by_user_id text NOT NULL,
    state text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    available_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    lease_token text,
    lease_until timestamp with time zone,
    fingerprint text,
    bytes_read bigint,
    error_code text,
    error_detail text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    finished_at timestamp with time zone,
    processing_started_at timestamp with time zone,
    processing_duration_ms bigint DEFAULT 0 NOT NULL,
    job_kind text DEFAULT 'combined'::text NOT NULL,
    CONSTRAINT video_phash_jobs_kind_check CHECK ((job_kind = ANY (ARRAY['combined'::text, 'phash'::text, 'thumbnail'::text]))),
    CONSTRAINT video_phash_jobs_attempts_check CHECK ((attempts >= 0)),
    CONSTRAINT video_phash_jobs_bytes_read_check CHECK (((bytes_read IS NULL) OR (bytes_read >= 0))),
    CONSTRAINT video_phash_jobs_check CHECK (((state = 'processing'::text) = ((lease_token IS NOT NULL) AND (lease_until IS NOT NULL)))),
    CONSTRAINT video_phash_jobs_duration_us_check CHECK (((duration_us >= 1) AND (duration_us <= '86400000000'::bigint))),
    CONSTRAINT video_phash_jobs_fingerprint_check CHECK (((fingerprint IS NULL) OR (fingerprint ~ '^[0-9a-f]{16}$'::text))),
    CONSTRAINT video_phash_jobs_processing_duration_ms_check CHECK ((processing_duration_ms >= 0)),
    CONSTRAINT video_phash_jobs_state_check CHECK ((state = ANY (ARRAY['queued'::text, 'processing'::text, 'done'::text, 'failed'::text, 'cancelled'::text])))
);

CREATE TABLE public.video_phash_provenance (
    video_id text NOT NULL,
    fingerprint text NOT NULL,
    generator_version text NOT NULL,
    decoder_profile text NOT NULL,
    submitted_by_user_id text NOT NULL,
    submitted_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT video_phash_provenance_fingerprint_check CHECK ((fingerprint ~ '^[0-9a-f]{16}$'::text))
);

CREATE TABLE public.video_phash_thumbnails (
    video_id text NOT NULL,
    image_bytes bytea NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE TABLE public.video_sources (
    id bigint NOT NULL,
    video_id text NOT NULL,
    source_id text NOT NULL,
    provider text NOT NULL,
    provider_ref text,
    source_url text,
    channel_id text,
    priority integer DEFAULT 1,
    is_active integer DEFAULT 1,
    created_at timestamp with time zone NOT NULL
);

ALTER TABLE public.video_sources ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.video_sources_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.video_visual_comparisons (
    id text NOT NULL,
    left_id text NOT NULL,
    right_id text NOT NULL,
    algorithm_version text NOT NULL,
    left_source_key text NOT NULL,
    right_source_key text NOT NULL,
    state text NOT NULL,
    evidence jsonb DEFAULT '{}'::jsonb NOT NULL,
    frame_sheet bytea,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    expires_at bigint,
    CONSTRAINT video_visual_comparisons_check CHECK ((left_id < right_id)),
    CONSTRAINT video_visual_comparisons_state_check CHECK ((state = ANY (ARRAY['queued'::text, 'processing'::text, 'confirmed'::text, 'review'::text, 'different'::text, 'inconclusive'::text, 'failed'::text])))
);

CREATE TABLE public.video_visual_history (
    id bigint NOT NULL,
    kind text NOT NULL,
    record jsonb NOT NULL,
    image bytea,
    archived_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE SEQUENCE public.video_visual_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.video_visual_history_id_seq OWNED BY public.video_visual_history.id;

CREATE TABLE public.video_visual_jobs (
    id text NOT NULL,
    dedupe_key text NOT NULL,
    kind text NOT NULL,
    video_id text,
    left_id text,
    right_id text,
    source_key text DEFAULT ''::text NOT NULL,
    right_source_key text DEFAULT ''::text NOT NULL,
    algorithm_version text NOT NULL,
    state text NOT NULL,
    priority smallint DEFAULT 20 NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    available_at bigint DEFAULT (EXTRACT(epoch FROM CURRENT_TIMESTAMP))::bigint NOT NULL,
    lease_token text,
    lease_until bigint,
    error_code text,
    error_detail text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    finished_at bigint,
    CONSTRAINT video_visual_jobs_attempts_check CHECK ((attempts >= 0)),
    CONSTRAINT video_visual_jobs_check CHECK ((((kind = 'signature'::text) AND (video_id IS NOT NULL) AND (left_id IS NULL) AND (right_id IS NULL)) OR ((kind = 'verify_pair'::text) AND (video_id IS NULL) AND (left_id IS NOT NULL) AND (right_id IS NOT NULL) AND (left_id < right_id)))),
    CONSTRAINT video_visual_jobs_check1 CHECK (((state = 'processing'::text) = ((lease_token IS NOT NULL) AND (lease_until IS NOT NULL)))),
    CONSTRAINT video_visual_jobs_kind_check CHECK ((kind = ANY (ARRAY['signature'::text, 'verify_pair'::text]))),
    CONSTRAINT video_visual_jobs_priority_check CHECK (((priority >= 0) AND (priority <= 100))),
    CONSTRAINT video_visual_jobs_state_check CHECK ((state = ANY (ARRAY['queued'::text, 'processing'::text, 'done'::text, 'failed'::text, 'cancelled'::text])))
);

CREATE TABLE public.video_visual_signatures (
    video_id text NOT NULL,
    algorithm_version text NOT NULL,
    source_key text NOT NULL,
    duration_seconds double precision NOT NULL,
    signature jsonb NOT NULL,
    contact_sheet bytea,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE TABLE public.video_visual_state (
    id smallint NOT NULL,
    algorithm_version text NOT NULL,
    report_started_at bigint NOT NULL,
    automatic_enabled_at bigint,
    pair_cursor bigint DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT video_visual_state_id_check CHECK ((id = 1))
);

CREATE TABLE public.videos (
    id text NOT NULL,
    provider text,
    title text,
    slug text,
    duration integer,
    thumbnail text,
    performers jsonb,
    categories jsonb DEFAULT '[]'::jsonb NOT NULL,
    tags jsonb DEFAULT '[]'::jsonb NOT NULL,
    embed_host text,
    playable integer,
    studio text,
    performers_checked_at timestamp with time zone,
    performers_audited_at timestamp with time zone,
    plot text,
    date_released text,
    scene_matched_at timestamp with time zone,
    first_seen timestamp with time zone,
    last_seen timestamp with time zone,
    source_text text DEFAULT ''::text NOT NULL,
    source_filename text DEFAULT ''::text NOT NULL,
    scene_match_source text,
    scene_match_external_id text,
    scene_match_url text,
    scene_match_confidence double precision,
    performer_resolution jsonb,
    file_size_bytes bigint,
    CONSTRAINT videos_categories_array_ck CHECK (((categories IS NULL) OR public.adult_hub_is_text_array(categories))),
    CONSTRAINT videos_file_size_bytes_check CHECK (((file_size_bytes IS NULL) OR (file_size_bytes >= 0))),
    CONSTRAINT videos_minimum_duration_seconds CHECK (((duration IS NULL) OR (duration <= 0) OR (duration >= 420))),
    CONSTRAINT videos_performers_array_ck CHECK (((performers IS NULL) OR public.adult_hub_is_text_array(performers))),
    CONSTRAINT videos_tags_array_ck CHECK (((tags IS NULL) OR public.adult_hub_is_text_array(tags)))
);

ALTER TABLE ONLY public.admin_action_events ALTER COLUMN id SET DEFAULT nextval('public.admin_action_events_id_seq'::regclass);

ALTER TABLE ONLY public.admin_audit_events ALTER COLUMN id SET DEFAULT nextval('public.admin_audit_events_id_seq'::regclass);

ALTER TABLE ONLY public.metadata_review_proposals ALTER COLUMN id SET DEFAULT nextval('public.metadata_review_proposals_id_seq'::regclass);

ALTER TABLE ONLY public.performer_catalog_aliases ALTER COLUMN id SET DEFAULT nextval('public.performer_catalog_aliases_id_seq'::regclass);

ALTER TABLE ONLY public.telegram_performer_proposals ALTER COLUMN id SET DEFAULT nextval('public.telegram_performer_proposals_id_seq'::regclass);

ALTER TABLE ONLY public.video_visual_history ALTER COLUMN id SET DEFAULT nextval('public.video_visual_history_id_seq'::regclass);

ALTER TABLE ONLY public.admin_action_events
    ADD CONSTRAINT admin_action_events_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.admin_audit_events
    ADD CONSTRAINT admin_audit_events_event_key_key UNIQUE (event_key);

ALTER TABLE ONLY public.admin_audit_events
    ADD CONSTRAINT admin_audit_events_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.admin_audit_items
    ADD CONSTRAINT admin_audit_items_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.admin_operation_plans
    ADD CONSTRAINT admin_operation_plans_pkey PRIMARY KEY (plan_id);

ALTER TABLE ONLY public.admin_orphan_quarantine
    ADD CONSTRAINT admin_orphan_quarantine_pkey PRIMARY KEY (operation_id);

ALTER TABLE ONLY public.admin_quality_acknowledgements
    ADD CONSTRAINT admin_quality_acknowledgement_video_id_kind_finding_fingerp_key UNIQUE (video_id, kind, finding_fingerprint);

ALTER TABLE ONLY public.admin_quality_acknowledgements
    ADD CONSTRAINT admin_quality_acknowledgements_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.admin_schedules
    ADD CONSTRAINT admin_schedules_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.system_schedule_stages
    ADD CONSTRAINT system_schedule_stages_pkey PRIMARY KEY (schedule_id, id);

ALTER TABLE ONLY public.system_schedules
    ADD CONSTRAINT system_schedules_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.dead_streams
    ADD CONSTRAINT dead_streams_pkey PRIMARY KEY (video_id);

ALTER TABLE ONLY public.diagnostic_reports
    ADD CONSTRAINT diagnostic_reports_pkey PRIMARY KEY (report_id);

ALTER TABLE ONLY public.diagnostic_settings
    ADD CONSTRAINT diagnostic_settings_pkey PRIMARY KEY (key);

ALTER TABLE ONLY public.external_metadata_attempts
    ADD CONSTRAINT external_metadata_attempts_pkey PRIMARY KEY (entity_type, entity_key, source);

ALTER TABLE ONLY public.jellyfin_person_sync
    ADD CONSTRAINT jellyfin_person_sync_pkey PRIMARY KEY (name);

ALTER TABLE ONLY public.meta
    ADD CONSTRAINT meta_pkey PRIMARY KEY (key);

ALTER TABLE ONLY public.metadata_review_proposals
    ADD CONSTRAINT metadata_review_proposals_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.metadata_review_proposals
    ADD CONSTRAINT metadata_review_proposals_video_id_candidate_fingerprint_key UNIQUE (video_id, candidate_fingerprint);

ALTER TABLE ONLY public.performer_aliases
    ADD CONSTRAINT performer_aliases_pkey PRIMARY KEY (alias);

ALTER TABLE ONLY public.performer_bio_migration_issues
    ADD CONSTRAINT performer_bio_migration_issues_pkey PRIMARY KEY (name);

ALTER TABLE ONLY public.performer_bios
    ADD CONSTRAINT performer_bios_pkey PRIMARY KEY (name);

ALTER TABLE ONLY public.performer_catalog_aliases
    ADD CONSTRAINT performer_catalog_aliases_performer_id_normalized_origin_key UNIQUE (performer_id, normalized, origin);

ALTER TABLE ONLY public.performer_catalog_aliases
    ADD CONSTRAINT performer_catalog_aliases_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.performer_catalog
    ADD CONSTRAINT performer_catalog_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.performer_catalog
    ADD CONSTRAINT performer_catalog_source_source_id_key UNIQUE (source, source_id);

ALTER TABLE ONLY public.performer_photos
    ADD CONSTRAINT performer_photos_pkey PRIMARY KEY (name);

ALTER TABLE ONLY public.performer_profile_overrides
    ADD CONSTRAINT performer_profile_overrides_pkey PRIMARY KEY (name);

ALTER TABLE ONLY public.stream_cache
    ADD CONSTRAINT stream_cache_pkey PRIMARY KEY (cache_key);

ALTER TABLE ONLY public.synced_files
    ADD CONSTRAINT synced_files_pkey PRIMARY KEY (file_path);

ALTER TABLE ONLY public.telegram_channel_state
    ADD CONSTRAINT telegram_channel_state_pkey PRIMARY KEY (channel_id);

ALTER TABLE ONLY public.telegram_performer_proposals
    ADD CONSTRAINT telegram_performer_proposals_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.telegram_performer_proposals
    ADD CONSTRAINT telegram_performer_proposals_video_id_normalized_key UNIQUE (video_id, normalized);

ALTER TABLE ONLY public.telegram_performer_rebuild_backup
    ADD CONSTRAINT telegram_performer_rebuild_backup_pkey PRIMARY KEY (run_id, video_id);

ALTER TABLE ONLY public.telegram_unavailable_quarantine
    ADD CONSTRAINT telegram_unavailable_quarantine_pkey PRIMARY KEY (video_id);

ALTER TABLE ONLY public.video_availability_checks
    ADD CONSTRAINT video_availability_checks_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.video_categories
    ADD CONSTRAINT video_categories_pkey PRIMARY KEY (video_id, category);

ALTER TABLE ONLY public.video_duplicate_decisions
    ADD CONSTRAINT video_duplicate_decisions_pkey PRIMARY KEY (loser_id);

ALTER TABLE ONLY public.video_duplicate_dismissals
    ADD CONSTRAINT video_duplicate_dismissals_pkey PRIMARY KEY (left_id, right_id);

ALTER TABLE ONLY public.video_duplicate_jobs
    ADD CONSTRAINT video_duplicate_jobs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.video_fingerprints
    ADD CONSTRAINT video_fingerprints_pkey PRIMARY KEY (video_id, algorithm);

ALTER TABLE ONLY public.video_metadata_migration_issues
    ADD CONSTRAINT video_metadata_migration_issues_pkey PRIMARY KEY (video_id, field_name);

ALTER TABLE ONLY public.video_metadata_overrides
    ADD CONSTRAINT video_metadata_overrides_pkey PRIMARY KEY (video_id);

ALTER TABLE ONLY public.video_performer_migration_issues
    ADD CONSTRAINT video_performer_migration_issues_pkey PRIMARY KEY (video_id, performer_key);

ALTER TABLE ONLY public.video_performers
    ADD CONSTRAINT video_performers_pkey PRIMARY KEY (video_id, performer_name);

ALTER TABLE ONLY public.video_performers
    ADD CONSTRAINT video_performers_video_id_position_key UNIQUE (video_id, "position");

ALTER TABLE ONLY public.video_phash_jobs
    ADD CONSTRAINT video_phash_jobs_pkey PRIMARY KEY (id);

CREATE UNIQUE INDEX video_phash_jobs_identity_kind_idx ON public.video_phash_jobs USING btree (video_id, algorithm_version, decoder_profile, job_kind);

ALTER TABLE ONLY public.video_phash_provenance
    ADD CONSTRAINT video_phash_provenance_pkey PRIMARY KEY (video_id);

ALTER TABLE ONLY public.video_phash_thumbnails
    ADD CONSTRAINT video_phash_thumbnails_pkey PRIMARY KEY (video_id);

ALTER TABLE ONLY public.video_sources
    ADD CONSTRAINT video_sources_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.video_sources
    ADD CONSTRAINT video_sources_source_id_key UNIQUE (source_id);

ALTER TABLE ONLY public.video_visual_comparisons
    ADD CONSTRAINT video_visual_comparisons_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.video_visual_history
    ADD CONSTRAINT video_visual_history_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.video_visual_jobs
    ADD CONSTRAINT video_visual_jobs_dedupe_key_key UNIQUE (dedupe_key);

ALTER TABLE ONLY public.video_visual_jobs
    ADD CONSTRAINT video_visual_jobs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.video_visual_signatures
    ADD CONSTRAINT video_visual_signatures_pkey PRIMARY KEY (video_id);

ALTER TABLE ONLY public.video_visual_state
    ADD CONSTRAINT video_visual_state_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.videos
    ADD CONSTRAINT videos_pkey PRIMARY KEY (id);

CREATE INDEX idx_admin_action_events_created ON public.admin_action_events USING btree (created_at DESC, id DESC);

CREATE INDEX idx_admin_audit_events_created ON public.admin_audit_events USING btree (created_at DESC, id DESC);

CREATE INDEX idx_admin_audit_events_object ON public.admin_audit_events USING btree (object_type, object_id, created_at DESC);

CREATE INDEX idx_admin_audit_items_job ON public.admin_audit_items USING btree (job_id, id);

CREATE INDEX idx_admin_audit_items_kind ON public.admin_audit_items USING btree (kind, status, checked_at DESC);

CREATE INDEX idx_admin_audit_items_quality ON public.admin_audit_items USING btree (job_id, kind, status, id);

CREATE INDEX idx_admin_operation_plans_expiry ON public.admin_operation_plans USING btree (expires_at);

CREATE INDEX idx_admin_operation_plans_state ON public.admin_operation_plans USING btree (state, created_at DESC);

CREATE INDEX idx_admin_orphan_quarantine_history_retention ON public.admin_orphan_quarantine USING btree (created_at, operation_id) WHERE (state = 'restored'::text);

CREATE INDEX idx_admin_orphan_quarantine_state ON public.admin_orphan_quarantine USING btree (state, updated_at DESC);

CREATE INDEX idx_admin_quality_ack_video ON public.admin_quality_acknowledgements USING btree (video_id, kind, updated_at DESC);

CREATE INDEX idx_admin_schedules_due ON public.admin_schedules USING btree (next_run_at) WHERE (enabled = true);

CREATE INDEX idx_system_schedules_due ON public.system_schedules USING btree (next_run_at) WHERE (enabled = true);

CREATE INDEX idx_diagnostic_reports_ended ON public.diagnostic_reports USING btree (ended_at DESC);

CREATE INDEX idx_external_metadata_attempts_due ON public.external_metadata_attempts USING btree (entity_type, source, next_attempt_at, entity_key);

CREATE INDEX idx_external_metadata_attempts_history_retention ON public.external_metadata_attempts USING btree (updated_at, entity_type, entity_key, source);

CREATE INDEX idx_metadata_review_pending ON public.metadata_review_proposals USING btree (created_at, id) WHERE (status = 'pending'::text);
CREATE INDEX idx_metadata_review_retry ON public.metadata_review_proposals USING btree (next_attempt_at, id) WHERE (status = 'pending'::text);

CREATE INDEX idx_metadata_review_video ON public.metadata_review_proposals USING btree (video_id, status, created_at DESC);

CREATE INDEX idx_performer_alias_normalized ON public.performer_catalog_aliases USING btree (normalized);

CREATE INDEX idx_performer_aliases_canonical_lower ON public.performer_aliases USING btree (lower(canonical));

CREATE INDEX idx_sf_video_id ON public.synced_files USING btree (video_id);

CREATE INDEX idx_stream_cache_expire ON public.stream_cache USING btree (expire_at);

CREATE INDEX idx_stream_cache_lru ON public.stream_cache USING btree (last_accessed);

CREATE INDEX idx_telegram_performer_proposals_status ON public.telegram_performer_proposals USING btree (status, created_at);

CREATE INDEX idx_telegram_unavailable_channel_check ON public.telegram_unavailable_quarantine USING btree (channel_id, last_checked_at);

CREATE INDEX idx_vc_category ON public.video_categories USING btree (category);

CREATE INDEX idx_vc_video_id ON public.video_categories USING btree (video_id);

CREATE INDEX idx_vdj_queue ON public.video_duplicate_jobs USING btree (action, state, created_at);

CREATE INDEX idx_video_availability_history_retention ON public.video_availability_checks USING btree (checked_at, id);

CREATE UNIQUE INDEX idx_video_availability_job_item ON public.video_availability_checks USING btree (job_id, video_id);

CREATE INDEX idx_video_availability_latest ON public.video_availability_checks USING btree (video_id, checked_at DESC, id DESC);

CREATE INDEX idx_video_duplicate_decisions_history_retention ON public.video_duplicate_decisions USING btree (created_at, loser_id) WHERE (state = 'undone'::text);

CREATE INDEX idx_video_duplicate_decisions_keeper_id ON public.video_duplicate_decisions USING btree (keeper_id);

CREATE INDEX idx_video_duplicate_decisions_operation_state ON public.video_duplicate_decisions USING btree (operation_id, state);

CREATE INDEX idx_video_duplicate_dismissals_history_retention ON public.video_duplicate_dismissals USING btree (created_at, left_id, right_id) WHERE (active = 0);

CREATE INDEX idx_video_duplicate_dismissals_right_id ON public.video_duplicate_dismissals USING btree (right_id);

CREATE INDEX idx_video_duplicate_jobs_history_retention ON public.video_duplicate_jobs USING btree (created_at, id) WHERE (state = ANY (ARRAY['done'::text, 'failed'::text, 'cancelled'::text]));

CREATE INDEX idx_video_fingerprints_algorithm ON public.video_fingerprints USING btree (algorithm, fingerprint);

CREATE INDEX idx_video_metadata_overrides_updated ON public.video_metadata_overrides USING btree (updated_at DESC);

CREATE INDEX idx_video_performers_name ON public.video_performers USING btree (performer_name, video_id);

CREATE INDEX idx_videos_last_seen ON public.videos USING btree (last_seen);

CREATE INDEX idx_videos_provider ON public.videos USING btree (provider);

CREATE INDEX idx_vs_source_id ON public.video_sources USING btree (source_id);

CREATE INDEX idx_vs_video_id ON public.video_sources USING btree (video_id, is_active, priority);

CREATE INDEX video_phash_jobs_ready_idx ON public.video_phash_jobs USING btree (available_at, created_at, id) WHERE (state = 'queued'::text);

CREATE UNIQUE INDEX video_phash_jobs_single_active_idx ON public.video_phash_jobs USING btree ((1)) WHERE (state = 'processing'::text);

CREATE INDEX video_visual_comparisons_state_idx ON public.video_visual_comparisons USING btree (state, updated_at DESC);

CREATE INDEX video_visual_jobs_ready_idx ON public.video_visual_jobs USING btree (available_at, created_at, id) WHERE (state = 'queued'::text);

CREATE UNIQUE INDEX video_visual_jobs_single_active_idx ON public.video_visual_jobs USING btree ((1)) WHERE (state = 'processing'::text);

CREATE INDEX video_visual_signatures_duration_idx ON public.video_visual_signatures USING btree (duration_seconds, video_id);

CREATE TRIGGER archive_visual_comparison BEFORE DELETE OR UPDATE OF evidence, frame_sheet, left_source_key, right_source_key ON public.video_visual_comparisons FOR EACH ROW EXECUTE FUNCTION public.archive_video_visual_row();

CREATE TRIGGER archive_visual_signature BEFORE DELETE OR UPDATE OF source_key, algorithm_version, signature, contact_sheet ON public.video_visual_signatures FOR EACH ROW EXECUTE FUNCTION public.archive_video_visual_row();

CREATE TRIGGER videos_minimum_duration_insert BEFORE INSERT ON public.videos FOR EACH ROW EXECUTE FUNCTION public.reject_unverified_short_video_duration();

CREATE TRIGGER videos_minimum_duration_update BEFORE UPDATE OF duration ON public.videos FOR EACH ROW EXECUTE FUNCTION public.reject_unverified_short_video_duration();

CREATE TRIGGER videos_sync_video_performers AFTER INSERT OR UPDATE OF performers ON public.videos FOR EACH ROW EXECUTE FUNCTION public.adult_hub_sync_video_performers();

ALTER TABLE ONLY public.admin_audit_events
    ADD CONSTRAINT admin_audit_events_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.video_duplicate_jobs(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.admin_audit_items
    ADD CONSTRAINT admin_audit_items_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.video_duplicate_jobs(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.admin_orphan_quarantine
    ADD CONSTRAINT admin_orphan_quarantine_audit_id_fkey FOREIGN KEY (audit_id) REFERENCES public.video_duplicate_jobs(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.admin_orphan_quarantine
    ADD CONSTRAINT admin_orphan_quarantine_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.video_duplicate_jobs(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.metadata_review_proposals
    ADD CONSTRAINT metadata_review_proposals_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.videos(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.performer_catalog_aliases
    ADD CONSTRAINT performer_catalog_aliases_performer_id_fkey FOREIGN KEY (performer_id) REFERENCES public.performer_catalog(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.system_schedule_stages
    ADD CONSTRAINT system_schedule_stages_schedule_id_fkey FOREIGN KEY (schedule_id) REFERENCES public.system_schedules(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.telegram_performer_proposals
    ADD CONSTRAINT telegram_performer_proposals_performer_id_fkey FOREIGN KEY (performer_id) REFERENCES public.performer_catalog(id);

ALTER TABLE ONLY public.telegram_performer_proposals
    ADD CONSTRAINT telegram_performer_proposals_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.videos(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.telegram_unavailable_quarantine
    ADD CONSTRAINT telegram_unavailable_quarantine_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.videos(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.video_availability_checks
    ADD CONSTRAINT video_availability_checks_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.video_duplicate_jobs(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.video_availability_checks
    ADD CONSTRAINT video_availability_checks_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.videos(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.video_categories
    ADD CONSTRAINT video_categories_video_fk FOREIGN KEY (video_id) REFERENCES public.videos(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.video_fingerprints
    ADD CONSTRAINT video_fingerprints_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.videos(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.video_metadata_migration_issues
    ADD CONSTRAINT video_metadata_migration_issues_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.videos(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.video_metadata_overrides
    ADD CONSTRAINT video_metadata_overrides_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.videos(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.video_performer_migration_issues
    ADD CONSTRAINT video_performer_migration_issues_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.videos(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.video_performers
    ADD CONSTRAINT video_performers_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.videos(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.video_phash_jobs
    ADD CONSTRAINT video_phash_jobs_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.videos(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.video_phash_provenance
    ADD CONSTRAINT video_phash_provenance_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.videos(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.video_phash_thumbnails
    ADD CONSTRAINT video_phash_thumbnails_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.videos(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.video_visual_comparisons
    ADD CONSTRAINT video_visual_comparisons_left_id_fkey FOREIGN KEY (left_id) REFERENCES public.videos(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.video_visual_comparisons
    ADD CONSTRAINT video_visual_comparisons_right_id_fkey FOREIGN KEY (right_id) REFERENCES public.videos(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.video_visual_jobs
    ADD CONSTRAINT video_visual_jobs_left_id_fkey FOREIGN KEY (left_id) REFERENCES public.videos(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.video_visual_jobs
    ADD CONSTRAINT video_visual_jobs_right_id_fkey FOREIGN KEY (right_id) REFERENCES public.videos(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.video_visual_jobs
    ADD CONSTRAINT video_visual_jobs_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.videos(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.video_visual_signatures
    ADD CONSTRAINT video_visual_signatures_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.videos(id) ON DELETE CASCADE;
