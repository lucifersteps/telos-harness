// Native shadow prices keep Desktop's context meter consistent with replacements.
export function replaceSurface(session,meter,type,data,seqs) {
  if(typeof meter?.estimateMessage==='function')session.append('compaction/prune',{
    shadowedRange:{start:seqs[0],end:seqs.at(-1)},shadowedSeqs:seqs,
    shadowedTokenCount:seqs.reduce((sum,seq)=>sum+meter.estimateMessage(session.eventAt(seq).type==='user/message'?session.eventAt(seq).data:session.eventAt(seq).data.message),0)
  });
  return session.append(type,data,{surfaceOp:{op:'replace',startSeq:seqs[0],endSeq:seqs.at(-1)},sourceEventSeqs:seqs});
}
