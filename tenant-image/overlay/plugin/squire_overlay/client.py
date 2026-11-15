"""Scoped broker requests; persist egress before network submission."""
import json
import re
import time
import urllib.parse
import urllib.error
import urllib.request

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


class BrokerError(RuntimeError):
    def __init__(self,status): self.status=status;super().__init__("messaging dependency unavailable")

class Broker:
    def __init__(self,config): self.config=config
    def request(self,method,path,payload=None,*,timeout=20):
        request=urllib.request.Request(self.config.messaging_url+path,method=method,data=json.dumps(payload).encode() if payload is not None else None,headers={"Content-Type":"application/json","Authorization":"Bearer "+self.config.egress_token})
        try:
            with urllib.request.urlopen(request,timeout=timeout) as response:
                result=json.loads(response.read(1024*1024))
                if not isinstance(result,dict):raise BrokerError(502)
                return result
        except urllib.error.HTTPError as exc:raise BrokerError(exc.code) from None
        except (OSError,ValueError):raise BrokerError(503) from None

    def fetch_media(self,event_id):
        request=urllib.request.Request(self.config.messaging_url+"/v1/media/"+urllib.parse.quote(str(event_id),safe=""),headers={"Authorization":"Bearer "+self.config.egress_token})
        maximum=10*1024*1024;started=time.monotonic();chunks=[];size=0
        try:
            with urllib.request.build_opener(NoRedirect()).open(request,timeout=45) as response:
                if int(response.headers.get("Content-Length","0"))>maximum:raise BrokerError(413)
                mime=response.headers.get("Content-Type","application/octet-stream").split(";",1)[0].lower()
                if not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+",mime):mime="application/octet-stream"
                while True:
                    chunk=response.read(min(65536,maximum-size+1))
                    if not chunk:break
                    size+=len(chunk)
                    if size>maximum:raise BrokerError(413)
                    if time.monotonic()-started>60:raise BrokerError(503)
                    chunks.append(chunk)
                return b"".join(chunks),mime
        except urllib.error.HTTPError as exc:raise BrokerError(exc.code) from None
        except (OSError,ValueError):raise BrokerError(503) from None

    def flush(self,store):
        row=store.outbox()
        if not row:return False
        payload=json.loads(row["payload"])
        try:
            approval=payload.pop("_approval_id",None)
            if approval:
                result=self.request("POST",f"/v1/approvals/{approval}/result",{"tenant_id":self.config.tenant_id,"decision":"approve","result":payload["text"]})
                operation=result.get("approval_id")
            else:
                result=self.request("POST","/v1/outbound",payload)
                operation=result.get("operation_id")
            if not operation:raise BrokerError(502)
            store.outbox_result(row["response_key"],operation_id=operation)
        except BrokerError as exc:
            store.outbox_result(row["response_key"],terminal=exc.status in {400,401,403,404,410,422})
        return True
