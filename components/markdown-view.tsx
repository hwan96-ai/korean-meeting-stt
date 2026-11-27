import { Fragment, type ReactNode } from "react";
function inline(text:string):ReactNode[]{return text.split(/(\*\*[^*]+\*\*|<br\s*\/?>)/g).map((part,n)=>part.startsWith("**")?<strong key={n}>{part.slice(2,-2)}</strong>:/^<br/.test(part)?<br key={n}/>:<Fragment key={n}>{part.replace(/\\\|/g,"|")}</Fragment>);}
const cells=(line:string)=>line.trim().replace(/^\|/,"").replace(/\|$/,"").split(/(?<!\\)\|/).map(s=>s.trim());
export default function MarkdownView({content}:{content:string}){
 const lines=content.split(/\r?\n/),blocks:ReactNode[]=[];let i=0;
 while(i<lines.length){const line=lines[i].trim();if(!line){i++;continue;}const heading=/^(#{1,4}) (.+)$/.exec(line);if(heading){const Tag=`h${heading[1].length}` as "h1"|"h2"|"h3"|"h4";blocks.push(<Tag key={i}>{inline(heading[2])}</Tag>);i++;continue;}
  if(line.startsWith("|")&&i+1<lines.length&&/^\|?[\s:|-]+\|/.test(lines[i+1])){const key=i,headers=cells(line),rows=[];i+=2;while(i<lines.length&&lines[i].trim().startsWith("|")){rows.push(cells(lines[i]));i++;}blocks.push(<div className="markdown-table" key={key}><table><thead><tr>{headers.map((h,n)=><th key={n}>{inline(h)}</th>)}</tr></thead><tbody>{rows.map((row,n)=><tr key={n}>{headers.map((_,c)=><td key={c}>{inline(row[c]??"")}</td>)}</tr>)}</tbody></table></div>);continue;}
  if(/^[-*] /.test(line)){const key=i,items=[];while(i<lines.length&&/^\s*[-*] /.test(lines[i])){const nested=/^\s{2,}/.test(lines[i]);items.push(<li key={i} className={nested?"nested-item":undefined}>{inline(lines[i].trim().slice(2))}</li>);i++;}blocks.push(<ul key={key}>{items}</ul>);continue;}
  blocks.push(<p key={i}>{inline(line)}</p>);i++;
 }
 return <article className="markdown-preview">{blocks}</article>;
}
