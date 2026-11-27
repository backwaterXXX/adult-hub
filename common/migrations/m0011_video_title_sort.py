"""Migration 0011: a maintained lowercase title for index-backed sorting."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration

# ``sort=title`` orders by ``lower(COALESCE(override->>'title', title))``: a
# value that spans a join, so no plain index can answer it and every page sorts
# the whole catalog (~34 ms on 28k rows). A stored lowercase key removes that
# sort. Triggers keep it in step with both the video title and the metadata
# override, so the column never goes stale after a sync or an admin edit.
TITLE_SORT_VALUE = (
    "lower(COALESCE("
    "(SELECT o.fields ->> 'title' FROM public.video_metadata_overrides o "
    "WHERE o.video_id = {video} AND o.fields ? 'title'), {title}, ''))"
)

STATEMENTS = (
    "ALTER TABLE public.videos ADD COLUMN IF NOT EXISTS title_sort text",
    "UPDATE public.videos v SET title_sort = "
    + TITLE_SORT_VALUE.format(video="v.id", title="v.title"),
    "CREATE INDEX IF NOT EXISTS idx_videos_title_sort ON public.videos (title_sort, id)",
    "CREATE INDEX IF NOT EXISTS idx_videos_title_sort_desc "
    "ON public.videos (title_sort DESC, id ASC)",
    """CREATE OR REPLACE FUNCTION public.set_video_title_sort() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            NEW.title_sort := lower(COALESCE(
                (SELECT o.fields ->> 'title' FROM public.video_metadata_overrides o
                 WHERE o.video_id = NEW.id AND o.fields ? 'title'),
                NEW.title,
                ''));
            RETURN NEW;
        END;
    $$""",
    """CREATE OR REPLACE FUNCTION public.refresh_video_title_sort() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                UPDATE public.videos SET title_sort = lower(COALESCE(title, ''))
                WHERE id = OLD.video_id;
                RETURN OLD;
            END IF;
            UPDATE public.videos
            SET title_sort = lower(COALESCE(NEW.fields ->> 'title', title, ''))
            WHERE id = NEW.video_id;
            RETURN NEW;
        END;
    $$""",
    "DROP TRIGGER IF EXISTS videos_refresh_title_sort ON public.videos",
    "CREATE TRIGGER videos_refresh_title_sort BEFORE INSERT OR UPDATE OF title "
    "ON public.videos FOR EACH ROW EXECUTE FUNCTION public.set_video_title_sort()",
    "DROP TRIGGER IF EXISTS video_metadata_overrides_refresh_title_sort "
    "ON public.video_metadata_overrides",
    "CREATE TRIGGER video_metadata_overrides_refresh_title_sort "
    "AFTER INSERT OR UPDATE OF fields OR DELETE ON public.video_metadata_overrides "
    "FOR EACH ROW EXECUTE FUNCTION public.refresh_video_title_sort()",
)


def apply(conn: Any, **_kwargs) -> None:
    for statement in STATEMENTS:
        conn.execute(statement)


migration = Migration(
    version=11,
    name="0011_video_title_sort",
    description="Store a lowercase title key so the title sort can use an index",
    apply_fn=apply,
)
