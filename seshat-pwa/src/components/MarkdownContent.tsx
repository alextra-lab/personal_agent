'use client';

import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism';
import type { Components } from 'react-markdown';

interface MarkdownContentProps {
  content: string;
}

function CopyIcon() {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="currentColor" className="w-3.5 h-3.5">
      <path d="M5.5 3.5A1.5 1.5 0 0 1 7 2h2.879a1.5 1.5 0 0 1 1.06.44l2.122 2.12a1.5 1.5 0 0 1 .439 1.061V9.5A1.5 1.5 0 0 1 12 11H9.5a.5.5 0 0 1 0-1H12a.5.5 0 0 0 .5-.5V6H10a1 1 0 0 1-1-1V2.5H7a.5.5 0 0 0-.5.5v1a.5.5 0 0 1-1 0V3.5Z" />
      <path d="M4.5 6a1.5 1.5 0 0 0-1.5 1.5v5A1.5 1.5 0 0 0 4.5 14h3A1.5 1.5 0 0 0 9 12.5v-5A1.5 1.5 0 0 0 7.5 6h-3Zm0 1h3a.5.5 0 0 1 .5.5v5a.5.5 0 0 1-.5.5h-3a.5.5 0 0 1-.5-.5v-5a.5.5 0 0 1 .5-.5Z" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="currentColor" className="w-3.5 h-3.5">
      <path fillRule="evenodd" d="M12.416 3.376a.75.75 0 0 1 .208 1.04l-5 7.5a.75.75 0 0 1-1.154.114l-3-3a.75.75 0 0 1 1.06-1.06l2.353 2.353 4.431-6.647a.75.75 0 0 1 1.102-.3Z" clipRule="evenodd" />
    </svg>
  );
}

/** Code block with language label and copy button, styled like Claude.ai. */
function CodeBlock({ language, children }: { language: string; children: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(children);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // clipboard API unavailable — silent fail
    }
  };

  return (
    <div className="relative group/code rounded-lg overflow-hidden my-2 border border-slate-700/50">
      {/* Code block header bar */}
      <div className="flex items-center justify-between px-3 py-1.5 bg-slate-800 border-b border-slate-700/50">
        <span className="text-xs text-slate-500 font-mono">{language}</span>
        <button
          onClick={handleCopy}
          className={`flex items-center gap-1 text-xs px-2 py-0.5 rounded transition-colors ${
            copied
              ? 'text-emerald-400'
              : 'text-slate-500 hover:text-slate-300'
          }`}
          aria-label="Copy code"
        >
          {copied ? (
            <>
              <CheckIcon />
              <span>Copied</span>
            </>
          ) : (
            <>
              <CopyIcon />
              <span>Copy</span>
            </>
          )}
        </button>
      </div>
      <SyntaxHighlighter
        style={oneDark}
        language={language}
        PreTag="div"
        customStyle={{
          margin: 0,
          borderRadius: 0,
          fontSize: '0.75rem',
          background: '#0d1117',
        }}
      >
        {children}
      </SyntaxHighlighter>
    </div>
  );
}

const markdownComponents: Components = {
  code({ className, children }) {
    const isInline = !className;
    const languageMatch = className?.match(/language-(\w+)/);
    const language = languageMatch ? languageMatch[1] : 'text';

    if (isInline) {
      return (
        <code className="text-xs font-mono px-1 py-0.5 rounded bg-slate-700/60 text-slate-200">
          {children}
        </code>
      );
    }

    return (
      <CodeBlock language={language}>
        {String(children).replace(/\n$/, '')}
      </CodeBlock>
    );
  },
  a({ href, children }) {
    return (
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className="text-blue-400 underline underline-offset-2 hover:text-blue-300"
      >
        {children}
      </a>
    );
  },
};

/**
 * Renders markdown content from the assistant.
 *
 * Uses react-markdown + remark-gfm with syntax highlighting.
 * Code blocks include a language label and copy button.
 */
export function MarkdownContent({ content }: MarkdownContentProps) {
  return (
    <div
      className={[
        'prose prose-sm prose-invert max-w-none',
        'prose-p:my-1.5 prose-headings:mt-3 prose-headings:mb-1.5',
        'prose-ul:my-1.5 prose-ol:my-1.5 prose-li:my-0.5',
        'prose-pre:my-0 prose-pre:p-0 prose-pre:bg-transparent prose-pre:rounded-none',
        'prose-code:before:content-none prose-code:after:content-none',
        'prose-table:text-xs',
        'break-words',
      ].join(' ')}
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
        {content}
      </ReactMarkdown>
    </div>
  );
}
