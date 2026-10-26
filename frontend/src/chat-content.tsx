/** Format the existing text contract without interpreting source labels as HTML.
 * Historical messages remain text; layout never grants source authority.
 */
export function ChatContent({
  content,
  sourceTargets = {},
}: {
  content: string;
  sourceTargets?: Record<string, string>;
}) {
  function textWithSources(text: string) {
    return text.split(/(\bS[1-9]\d*\b)/g).map((part, index) =>
      sourceTargets[part] ? (
        <a
          key={index}
          href={`#${sourceTargets[part]}`}
          onClick={() => {
            document.getElementById(sourceTargets[part])?.setAttribute("open", "");
          }}
        >
          {part}
        </a>
      ) : (
        part
      ),
    );
  }
  const lines = content.split("\n");
  const groups: { numbered: boolean; lines: string[] }[] = [];
  for (const line of lines) {
    const numbered = /^\d+\.\s+\S/.test(line);
    const previous = groups.at(-1);
    if (previous?.numbered === numbered) previous.lines.push(line);
    else groups.push({ numbered, lines: [line] });
  }
  return (
    <div className="chat-content">
      {groups.map((group, index) =>
        group.numbered ? (
          <ol key={index} start={Number.parseInt(group.lines[0], 10)}>
            {group.lines.map((line, row) => (
              <li key={row}>{textWithSources(line.replace(/^\d+\.\s+/, ""))}</li>
            ))}
          </ol>
        ) : (
          <div key={index}>{textWithSources(group.lines.join("\n"))}</div>
        ),
      )}
    </div>
  );
}
