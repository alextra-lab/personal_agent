'use client';

import {
  useState,
  useRef,
  useCallback,
  type KeyboardEvent,
  type FormEvent,
  type ClipboardEvent,
  type DragEvent,
} from 'react';

import type { ExecutionProfile } from '@/lib/types';
import type { UploadedAttachment, UploadState } from '@/lib/agui-client';
import { presignUpload, uploadToR2, completeUpload } from '@/lib/agui-client';
import { useInferenceStatus } from '@/hooks/useInferenceStatus';

interface ChatInputProps {
  onSend: (text: string, attachments: UploadedAttachment[]) => void;
  disabled?: boolean;
  placeholder?: string;
  profile: ExecutionProfile;
  onProfileChange: (profile: ExecutionProfile) => void;
  /** While true the action button becomes a Stop control (ADR-0076). */
  isStreaming?: boolean;
  /** Invoked when the Stop button is tapped (sends USER_CANCEL). */
  onStop?: () => void;
}

const ACCEPTED_TYPES =
  'image/png,image/jpeg,image/gif,image/webp,image/svg+xml,application/pdf,text/plain,text/markdown,text/csv,application/json';
const ACCEPTED_TYPE_SET = new Set(ACCEPTED_TYPES.split(','));

/**
 * Chat input bar with textarea, inline model toggle, send button, and
 * user-upload support (FRE-369): file picker, drag-drop, paste-image.
 *
 * Behaviour:
 * - Cmd+Enter (macOS) or Ctrl+Enter (Win/Linux) sends; Enter inserts a newline.
 * - Paste: image files trigger upload; text falls through to the textarea.
 * - The textarea auto-grows up to 5 lines.
 * - Footer padding accounts for iOS home-indicator via safe-area-inset-bottom.
 * - Send is blocked while any upload is in-progress (status !== 'complete').
 */
export function ChatInput({
  onSend,
  disabled = false,
  placeholder = 'Message Seshat...',
  profile,
  onProfileChange,
  isStreaming = false,
  onStop,
}: ChatInputProps) {
  const [text, setText] = useState('');
  const [uploads, setUploads] = useState<UploadState[]>([]);
  const [isDragOver, setIsDragOver] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const inference = useInferenceStatus(profile);
  const pathUnavailable = inference.status === 'down';
  const activeLabel = profile === 'local' ? 'Local' : 'Cloud';
  const otherLabel = profile === 'local' ? 'Cloud' : 'Local';

  // ---------------------------------------------------------------------------
  // Upload flow
  // ---------------------------------------------------------------------------

  const startUpload = useCallback(async (file: File) => {
    const clientId = crypto.randomUUID();
    setUploads((prev) => [...prev, { id: clientId, file, status: 'uploading' }]);

    try {
      const { upload_url, artifact_id } = await presignUpload(file);
      await uploadToR2(upload_url, file);
      const attachment = await completeUpload(artifact_id);
      setUploads((prev) =>
        prev.map((u) =>
          u.id === clientId ? { ...u, status: 'complete', artifact_id: attachment.artifact_id } : u,
        ),
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Upload failed';
      setUploads((prev) =>
        prev.map((u) => (u.id === clientId ? { ...u, status: 'error', error: msg } : u)),
      );
    }
  }, []);

  const handleFiles = useCallback(
    (files: FileList | File[]) => {
      for (const file of Array.from(files)) {
        if (ACCEPTED_TYPE_SET.has(file.type)) {
          void startUpload(file);
        } else {
          // Show a visible error chip rather than silently dropping the file.
          // Browsers report empty type for .md, .csv, OS-drag, and extensionless sources.
          const clientId = crypto.randomUUID();
          const label = file.type ? `unsupported type: ${file.type}` : 'unknown file type';
          setUploads((prev) => [
            ...prev,
            { id: clientId, file, status: 'error', error: label },
          ]);
        }
      }
    },
    [startUpload],
  );

  const removeUpload = (id: string) => {
    setUploads((prev) => prev.filter((u) => u.id !== id));
  };

  // ---------------------------------------------------------------------------
  // Send
  // ---------------------------------------------------------------------------

  const handleSubmit = (e?: FormEvent) => {
    e?.preventDefault();
    const trimmed = text.trim();
    const anyUploading = uploads.some((u) => u.status === 'uploading');
    if (!trimmed || disabled || pathUnavailable || anyUploading) return;

    const completed: UploadedAttachment[] = uploads
      .filter((u) => u.status === 'complete' && u.artifact_id)
      .map((u) => ({
        artifact_id: u.artifact_id!,
        content_type: u.file.type,
        title: u.file.name,
      }));

    onSend(trimmed, completed);
    setText('');
    setUploads([]);
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  };

  // ---------------------------------------------------------------------------
  // Keyboard / change / paste / drag
  // ---------------------------------------------------------------------------

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== 'Enter') return;
    if (!(e.metaKey || e.ctrlKey)) return;
    e.preventDefault();
    handleSubmit();
  };

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setText(e.target.value);
    const el = e.target;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
  };

  const handlePaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    // Check for image files FIRST; only then prevent default (FRE-369 guard order).
    const files = Array.from(e.clipboardData.files).filter((f) =>
      ACCEPTED_TYPE_SET.has(f.type),
    );
    if (files.length > 0) {
      e.preventDefault();
      handleFiles(files);
      return;
    }
    // No image files — fall through to plain-text normalisation.
    e.preventDefault();
    const plain = e.clipboardData.getData('text/plain');
    const el = textareaRef.current;
    if (!el) return;
    const start = el.selectionStart;
    const end = el.selectionEnd;
    const newValue = text.slice(0, start) + plain + text.slice(end);
    setText(newValue);
    requestAnimationFrame(() => {
      el.selectionStart = el.selectionEnd = start + plain.length;
      el.style.height = 'auto';
      el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
    });
  };

  const handleDragOver = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragOver(true);
  };

  const handleDragLeave = () => setIsDragOver(false);

  const handleDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragOver(false);
    if (e.dataTransfer.files.length > 0) {
      handleFiles(e.dataTransfer.files);
    }
  };

  // ---------------------------------------------------------------------------
  // Render helpers
  // ---------------------------------------------------------------------------

  const toggleProfile = () => {
    onProfileChange(profile === 'local' ? 'cloud' : 'local');
  };

  const anyUploading = uploads.some((u) => u.status === 'uploading');
  const canSend = text.trim().length > 0 && !disabled && !pathUnavailable && !anyUploading;

  return (
    <div
      className={`border-t border-slate-800 bg-slate-900 ${isDragOver ? 'ring-2 ring-inset ring-slate-500' : ''}`}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {/* Why Send is disabled — name the unavailable path + offer the switch. */}
      {pathUnavailable && (
        <button
          type="button"
          onClick={toggleProfile}
          className="w-full px-4 pt-2 text-left text-xs text-amber-300/90 hover:text-amber-200 transition-colors"
        >
          <span className="inline-block w-1.5 h-1.5 rounded-full bg-red-500 mr-1.5 align-middle" />
          {activeLabel} is currently unavailable. Tap to switch to {otherLabel}.
        </button>
      )}

      {/* Attachment chips */}
      {uploads.length > 0 && (
        <div className="flex flex-wrap gap-1.5 px-4 pt-2">
          {uploads.map((u) => (
            <div
              key={u.id}
              className={`flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full border ${
                u.status === 'error'
                  ? 'border-red-700 bg-red-950 text-red-300'
                  : u.status === 'complete'
                    ? 'border-emerald-800 bg-emerald-950 text-emerald-300'
                    : 'border-slate-700 bg-slate-800 text-slate-400'
              }`}
            >
              {u.status === 'uploading' && (
                <svg
                  className="animate-spin h-3 w-3"
                  xmlns="http://www.w3.org/2000/svg"
                  fill="none"
                  viewBox="0 0 24 24"
                >
                  <circle
                    className="opacity-25"
                    cx="12"
                    cy="12"
                    r="10"
                    stroke="currentColor"
                    strokeWidth="4"
                  />
                  <path
                    className="opacity-75"
                    fill="currentColor"
                    d="M4 12a8 8 0 018-8v8H4z"
                  />
                </svg>
              )}
              {u.status === 'complete' && <span>✓</span>}
              {u.status === 'error' && <span>✗</span>}
              <span className="max-w-[120px] truncate">{u.file.name}</span>
              <button
                type="button"
                onClick={() => removeUpload(u.id)}
                className="ml-0.5 opacity-60 hover:opacity-100"
                aria-label={`Remove ${u.file.name}`}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}

      <form
        onSubmit={handleSubmit}
        className="flex items-center gap-2 px-4 pt-3"
        style={{ paddingBottom: 'max(env(safe-area-inset-bottom, 0px), 0.5rem)' }}
      >
        {/* Compact model toggle */}
        <button
          type="button"
          onClick={toggleProfile}
          className="flex-shrink-0 flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-lg bg-slate-800 border border-slate-700 text-slate-400 hover:border-slate-500 hover:text-slate-200 transition-colors whitespace-nowrap"
          title={profile === 'local' ? 'Switch to Cloud (Claude Sonnet)' : 'Switch to Local (Qwen)'}
        >
          <span
            className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
              pathUnavailable
                ? 'bg-red-500'
                : profile === 'local'
                  ? 'bg-emerald-400'
                  : 'bg-amber-400'
            }`}
          />
          {profile === 'local' ? 'Local' : 'Cloud'}
        </button>

        {/* Hidden file input */}
        <input
          ref={fileInputRef}
          type="file"
          accept={ACCEPTED_TYPES}
          multiple
          className="hidden"
          onChange={(e) => {
            if (e.target.files) {
              handleFiles(e.target.files);
              e.target.value = '';
            }
          }}
        />

        {/* Paperclip attach button */}
        <button
          type="button"
          onClick={() => fileInputRef.current?.click()}
          aria-label="Attach file"
          className="flex-shrink-0 w-8 h-8 flex items-center justify-center rounded-lg text-slate-500 hover:text-slate-300 hover:bg-slate-800 transition-colors"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            viewBox="0 0 20 20"
            fill="currentColor"
            className="w-4 h-4"
          >
            <path
              fillRule="evenodd"
              d="M15.621 4.379a3 3 0 00-4.242 0l-7 7a1.5 1.5 0 002.122 2.121L13.243 7a.75.75 0 011.06 1.061l-6.742 6.5A3 3 0 013.318 9.379l7-7a4.5 4.5 0 016.364 6.364l-7 7A6 6 0 011.19 7.257l6.5-6.5a.75.75 0 011.06 1.061l-6.5 6.5a4.5 4.5 0 006.364 6.363l7-7a3 3 0 000-4.242z"
              clipRule="evenodd"
            />
          </svg>
        </button>

        <textarea
          ref={textareaRef}
          value={text}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          placeholder={placeholder}
          rows={1}
          className={`
            flex-1 resize-none rounded-2xl px-4 py-3 text-sm
            bg-slate-800 border border-slate-700
            text-slate-100 placeholder-slate-500
            focus:outline-none focus:border-slate-500 focus:ring-1 focus:ring-slate-500/30
            disabled:opacity-50 disabled:cursor-not-allowed
            transition-colors duration-150
            min-h-[44px] max-h-[140px]
          `}
        />

        {isStreaming ? (
          <button
            type="button"
            onClick={() => onStop?.()}
            aria-label="Stop generating"
            className="flex-shrink-0 w-9 h-9 rounded-full flex items-center justify-center transition-all duration-150 bg-white hover:bg-slate-100 text-slate-900 cursor-pointer"
          >
            <svg
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 20 20"
              fill="currentColor"
              className="w-4 h-4"
            >
              <rect x="5" y="5" width="10" height="10" rx="1.5" />
            </svg>
          </button>
        ) : (
          <button
            type="submit"
            disabled={!canSend}
            aria-label="Send message"
            title="Send (⌘+Enter)"
            className={`
              flex-shrink-0 w-9 h-9 rounded-full flex items-center justify-center
              transition-all duration-150
              ${
                canSend
                  ? 'bg-white hover:bg-slate-100 text-slate-900 cursor-pointer'
                  : 'bg-slate-700 text-slate-500 cursor-not-allowed'
              }
            `}
          >
            <svg
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 20 20"
              fill="currentColor"
              className="w-5 h-5"
            >
              <path
                fillRule="evenodd"
                d="M10 17a.75.75 0 01-.75-.75V5.612L5.29 9.77a.75.75 0 01-1.08-1.04l5.25-5.5a.75.75 0 011.08 0l5.25 5.5a.75.75 0 11-1.08 1.04L10.75 5.612V16.25A.75.75 0 0110 17z"
                clipRule="evenodd"
              />
            </svg>
          </button>
        )}
      </form>
    </div>
  );
}
