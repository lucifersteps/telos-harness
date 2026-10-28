"""Current-conversation image sharing with durable receipts for each message part."""
import asyncio
import hashlib
import json
from telos.adapters.wechat.image_outbound import download_image, upload_image, send_item, public_url, ImageAPIRejected


async def share_image(channel, data):
    session, call_id = data['session'], data['id']
    ref = channel.known_ref(session)
    args = data['args']
    image_url = public_url(args['image_url'])
    title, summary, link = (args.get(k,'').strip() for k in ('title','summary','link_url'))
    if link: public_url(link)
    if len(title)>150 or len(summary)>1000: raise ValueError('Title or summary too long')
    caption = '\n'.join(part for part in (title,summary,link) if part)
    if len(caption)>1800: raise ValueError('Caption including link must be at most 1800 characters')
    if not isinstance(call_id,str) or not call_id: raise ValueError('Tool call ID required')
    fingerprint=hashlib.sha256(json.dumps(args,sort_keys=True).encode()).hexdigest()
    delivery_id=hashlib.sha256((session+'\0'+call_id).encode()).hexdigest()
    async with channel.delivery_locks.setdefault(session,asyncio.Lock()):
        with channel.business.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS image_receipts (id TEXT PRIMARY KEY, fingerprint TEXT, state TEXT, result TEXT)')
            row=db.execute('SELECT fingerprint,state,result FROM image_receipts WHERE id=?',(delivery_id,)).fetchone()
        if row:
            if row[0]!=fingerprint: raise ValueError('Tool call ID was already used with different arguments')
            if row[2]: return json.loads(row[2])
            return {'status':'unknown','image':row[1], 'caption':'unknown',
                    'message':'Previous send was interrupted. Do not automatically resend; ask the user whether it arrived.'}
        def save(state,result=None):
            with channel.business.db() as db:
                db.execute('INSERT OR REPLACE INTO image_receipts VALUES(?,?,?,?)',
                           (delivery_id,fingerprint,state,json.dumps(result) if result else None))
        config=channel.adapter.config
        content=await download_image(image_url)
        item=await upload_image(config,ref.chat_id,content)
        # Persist before the first send. Timeout/crash is ambiguous and must not cause automatic resend.
        save('sending_image')
        try:
            receipt=await send_item(config,ref.chat_id,channel.adapter._context_token_for_ref(ref),item,'telos-image-'+delivery_id[:32])
            if receipt.get('context_reset'):channel.adapter._clear_context_token_for_ref(ref)
        except Exception as error:
            rejected=isinstance(error,ImageAPIRejected)
            result={'status':'failed' if rejected else 'unknown','image':'rejected' if rejected else 'unknown','caption':'not_sent',
                    'error':str(error) if rejected else type(error).__name__,
                    'requires_new_message':bool(rejected and error.prepare_failed),
                    'message':('WeChat returned prepare failed. Ask the user to send a new WeChat message before another attempt; the reply window may be exhausted.' if rejected and error.prepare_failed else 'Image send was not confirmed. Do not retry automatically or claim success.')}
            save('image_unknown',result);return result
        save('image_api_accepted')
        if caption:
            try:
                await send_item(config,ref.chat_id,channel.adapter._context_token_for_ref(ref),
                    {'type':1,'text_item':{'text':caption}},'telos-caption-'+delivery_id[:32])
            except Exception as error:
                result={'status':'partial','image':'api_accepted','caption':'rejected' if isinstance(error,ImageAPIRejected) else 'unknown',
                        'error':str(error) if isinstance(error,ImageAPIRejected) else type(error).__name__,
                        'message':'Image accepted; caption send not confirmed. Do not resend the image.'}
                save('caption_unknown',result);return result
        result={'status':'sent','image':'api_accepted','caption':'api_accepted' if caption else 'not_requested',
                'message':'WeChat API accepted the messages; device display is not independently confirmed.'}
        save('complete',result);return result
