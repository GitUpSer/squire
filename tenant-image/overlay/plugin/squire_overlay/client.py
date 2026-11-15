"""Scoped broker requests; persist egress before network submission."""
import json
import urllib.error
import urllib.request

class BrokerError(RuntimeError):
    def __init__(self,status): self.status=status;super().__init__("messaging dependency unavailable")

class Broker:
    def __init__(self,config): self.config=config
    def request(self,method,path,payload=None):
        request=urllib.request.Request(self.config.messaging_url+path,method=method,data=json.dumps(payload).encode() if payload is not None else None,headers={"Content-Type":"application/json","Authorization":"Bearer "+self.config.egress_token})
        try:
            with urllib.request.urlopen(request,timeout=20) as response:
                result=json.loads(response.read(1024*1024))
                if not isinstance(result,dict):raise BrokerError(502)
                return result
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
