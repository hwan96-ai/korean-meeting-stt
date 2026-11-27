// Demand-driven concatenation: never drain a whole recording into an HTTP queue.
export function streamParts<T>(parts: readonly T[], open: (part: T) => Promise<ReadableStream<Uint8Array>>) {
  let index=0,reader:ReadableStreamDefaultReader<Uint8Array>|undefined,cancelled=false;
  return new ReadableStream<Uint8Array>({
    async pull(controller){
      try{
        while(!cancelled){
          if(!reader){
            if(index===parts.length){controller.close();return;}
            reader=(await open(parts[index++])).getReader();
            if(cancelled){await reader.cancel();reader.releaseLock();reader=undefined;return;}
          }
          const chunk=await reader.read();
          if(cancelled)return;
          if(chunk.done){reader.releaseLock();reader=undefined;continue;}
          controller.enqueue(chunk.value);return;
        }
      }catch(error){if(reader){await reader.cancel().catch(()=>{});reader.releaseLock();reader=undefined;}if(!cancelled)controller.error(error);}
    },
    async cancel(reason){cancelled=true;if(reader){await reader.cancel(reason);reader.releaseLock();reader=undefined;}},
  },{highWaterMark:64*1024,size:chunk=>chunk.byteLength});
}
