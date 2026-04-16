'use client';

import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism';
import type { Components } from 'react-markdown';

interface MarkdownContentProps {
  content: string;
}

const markdownComponents: Components = {
  // Code blocks — SyntaxHighlighter for fenced code, pill for inline
  code({ className, children, ...props }) {
    const isInline = !className;
    const languageMatch = className?.match(/language-(\w+)/);
    const language = languageMatch ? languageMatch[1] : 'text';

    if (isInline) {
      return (
        <code className="text-xs font-mono" {...props}>
          {children}
        </code>
      );
    }

    return (
      <SyntaxHighlighter
        style={oneDark}
        language={language}
        PreTag="div"
        customStyle={{
          borderRadius: '0.5rem',
          fontSize: '0.75rem',
          margin: '0.5rem 0',
        }}
      >
        {String(children).replace(/\n$/, '')}
      </SyntaxHighlighter>
    );
  },
  // Open links in new tab
  a({ href, children }) {
    return (
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className="text-blue-400 underline hover:text-blue-300"
      >
        {children}
      </a>
    );
  },
};

/**
 * Renders markdown content from the LLM assistant.
 *
 * Uses react-markdown + remark-gfm (tables, strikethrough, task lists)
 * with syntax highlighting via react-syntax-highlighter.
 * Styled with Tailwind Typography prose classes (dark theme).
 */
export function MarkdownContent({ content }: MarkdownContentProps) {
  return (
    <div
      className={[
        'prose prose-sm prose-invert max-w-none',
        'prose-p:my-1 prose-headings:mt-2 prose-headings:mb-1',
        'prose-ul:my-1 prose-ol:my-1 prose-li:my-0',
        'prose-pre:my-1 prose-pre:p-0 prose-pre:bg-transparent',
        'prose-table:text-xs',
        'break-words',
      ].join(' ')}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={markdownComponents}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
