import Markdown, { defaultUrlTransform } from 'react-markdown'
import remarkGfm from 'remark-gfm'

interface SafeMarkdownProps {
  content: string
  streaming?: boolean
}

export function SafeMarkdown({ content, streaming = false }: SafeMarkdownProps) {
  return (
    <div className={`safe-markdown${streaming ? ' is-streaming' : ''}`}>
      <Markdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        urlTransform={defaultUrlTransform}
        components={{
          a: ({ children, node: _node, ...props }) => (
            <a {...props} target="_blank" rel="noreferrer noopener">{children}</a>
          ),
        }}
      >
        {content}
      </Markdown>
    </div>
  )
}
