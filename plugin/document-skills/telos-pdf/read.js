// Thin macOS framework adapter invoked by DSH's native bash tool; no parser/state.
ObjC.import('Foundation');
ObjC.import('PDFKit');
function run(args) {
  if(args.length<1||args.length>4)throw Error('Usage: read.js PDF [first last [characterOffset]]');
  const first=Number(args[1]||1),last=Number(args[2]||first),offset=Number(args[3]||0);
  if(!Number.isInteger(first)||!Number.isInteger(last)||first<1||last<first||last-first>=10||
     !Number.isInteger(offset)||offset<0||(offset&&first!==last))throw Error('Invalid page range or offset');
  const doc=$.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(args[0]));
  if(!doc||doc.isNil())throw Error('Invalid or unreadable PDF');
  if(doc.isLocked)throw Error('Encrypted PDF: send an unlocked copy');
  const count=Number(doc.pageCount);
  if(first>count)throw Error('Page out of range; total pages: '+count);
  const pages=[];let budget=24000;
  for(let page=first;page<=Math.min(last,count);page++) {
    const raw=doc.pageAtIndex(page-1).string;
    const text=raw&&!raw.isNil()?ObjC.unwrap(raw):'';
    if(offset>text.length)throw Error('Character offset exceeds page text');
    const content=text.slice(offset,offset+budget);
    pages.push({page,text:content,noText:!text,truncated:offset+content.length<text.length,
      nextCharacterOffset:offset+content.length});
    budget-=content.length;
    if(budget<=0)break;
  }
  return JSON.stringify({totalPages:count,pages,nextPage:pages[pages.length-1].page<count?pages[pages.length-1].page+1:null,
    limitation:'Text layer only; no OCR or visual-layout guarantee.'});
}
