"""Keep previous visual evidence when current rows change or videos are hidden."""
from common.migrations.base import Migration


def apply(conn, **kwargs):
    conn.execute('''CREATE TABLE IF NOT EXISTS video_visual_history (
        id BIGSERIAL PRIMARY KEY, kind TEXT NOT NULL, record JSONB NOT NULL,
        image BYTEA, archived_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    conn.execute('''CREATE OR REPLACE FUNCTION archive_video_visual_row() RETURNS trigger AS $$
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
    END; $$ LANGUAGE plpgsql''')
    conn.execute('DROP TRIGGER IF EXISTS archive_visual_signature ON video_visual_signatures')
    conn.execute('''CREATE TRIGGER archive_visual_signature BEFORE DELETE OR UPDATE OF source_key,algorithm_version,signature,contact_sheet
        ON video_visual_signatures FOR EACH ROW EXECUTE FUNCTION archive_video_visual_row()''')
    conn.execute('DROP TRIGGER IF EXISTS archive_visual_comparison ON video_visual_comparisons')
    conn.execute('''CREATE TRIGGER archive_visual_comparison BEFORE DELETE OR UPDATE OF evidence,frame_sheet,left_source_key,right_source_key
        ON video_visual_comparisons FOR EACH ROW EXECUTE FUNCTION archive_video_visual_row()''')


migration = Migration(53, '0053_visual_history', 'Preserve visual duplicate history', apply)
current_migration = Migration(5, '0005_visual_history', 'Preserve visual duplicate history', apply)
