-- FRE-369: add upload_pending flag to track in-flight user uploads.
-- TRUE while the browser is uploading to R2; FALSE once /api/uploads/{id}/complete
-- is called. Pending rows are excluded from all public artifact queries.
ALTER TABLE artifacts
    ADD COLUMN IF NOT EXISTS upload_pending BOOLEAN NOT NULL DEFAULT FALSE;
