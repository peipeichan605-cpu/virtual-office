"""Small local event client. No model API or remote account credential is used."""
import json
from pathlib import Path
import urllib.parse
import urllib.request


def validate_endpoint(value):
    endpoint=urllib.parse.urlsplit(value)
    if endpoint.scheme!='http' or endpoint.hostname not in {'127.0.0.1','localhost'} or endpoint.path!='/api/agents/events' or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError('Credential must point to the local Office event endpoint')
    return endpoint


def load_credential(path):
    credential=json.loads(Path(path).expanduser().read_text())
    validate_endpoint(credential['endpoint'])
    return credential


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, target):
        raise ValueError('Office event credential redirects are not allowed')


def send(credential,event):
    validate_endpoint(credential['endpoint'])
    req=urllib.request.Request(credential['endpoint'],data=json.dumps(event,ensure_ascii=False).encode(),method='POST',
        headers={'Content-Type':'application/json','X-Agent-Id':credential['agent_id'],'Authorization':'Bearer '+credential['token']})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    with opener.open(req,timeout=3) as response:return json.load(response)
