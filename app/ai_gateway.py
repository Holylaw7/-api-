"""Shared AI profiles and bounded API calls; no paid retry, redirect or fallback."""
from __future__ import annotations
import copy
import json
import math
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from .config import credential_dir

MAX_RESPONSE_BYTES = 4_000_000
MAX_INPUT_BYTES = 1_000_000
MAX_INPUT_CHARS = 200_000
MAX_MESSAGES = 100
MAX_PROFILE_BYTES = 1_000_000
LOCK_TIMEOUT = 5.0
PROVIDERS = {
    'deepseek': {'label':'DeepSeek','base_url':'https://api.deepseek.com','model':'deepseek-flash',
                 'protocol':'chat_completions','models':['deepseek-flash','deepseek-v4-pro'],'env':'DEEPSEEK_API_KEY'},
    'openai': {'label':'ChatGPT（OpenAI API）','base_url':'https://api.openai.com/v1','model':'gpt-5.6-terra',
               'protocol':'responses','models':['gpt-5.6-terra','gpt-5.6-luna','gpt-6-astra'],'env':'OPENAI_API_KEY'},
    'custom': {'label':'自定义兼容服务','base_url':'','model':'','protocol':'chat_completions',
               'models':[],'env':'CUSTOM_AI_API_KEY'},
}
_THREAD_LOCK = threading.RLock()
_MODEL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z')

class AIGatewayError(ValueError):
    """Safe short error; never include upstream bodies, headers or credentials."""

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AIGatewayError('模型服务返回重定向，已停止请求；请核对服务基地址')

def _provider_id(value):
    if not isinstance(value,str) or value not in PROVIDERS:
        raise AIGatewayError('请选择 DeepSeek、OpenAI 或自定义服务')
    return value

def _key(value):
    if not isinstance(value,str):
        raise AIGatewayError('API Key 格式无效')
    value=value.strip()
    if len(value)>4096 or not value.isascii() or any(c.isspace() or ord(c)<32 or ord(c)==127 for c in value):
        raise AIGatewayError('API Key 格式无效')
    return value

def _model(value, allow_empty=False):
    if not isinstance(value,str):
        raise AIGatewayError('模型名称格式无效')
    value=value.strip()
    if not value and allow_empty:
        return ''
    if not _MODEL_ID.fullmatch(value):
        raise AIGatewayError('请输入有效模型 ID，长度不超过 200 字符')
    return value

def _base_url(value, allow_empty=False):
    if not isinstance(value,str):
        raise AIGatewayError('模型服务基地址格式无效')
    value=value.strip().rstrip('/')
    if not value and allow_empty:
        return ''
    if len(value)>2048 or any(c.isspace() or ord(c)<32 for c in value):
        raise AIGatewayError('模型服务基地址格式无效')
    try:
        parsed=urlsplit(value)
        parsed.port
    except ValueError:
        raise AIGatewayError('模型服务基地址格式无效') from None
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AIGatewayError('基地址须为 HTTP(S) 地址，不能包含用户凭据、查询参数或片段')
    if parsed.scheme=='http' and parsed.hostname not in ('localhost','127.0.0.1','::1'):
        raise AIGatewayError('远程模型服务必须使用 HTTPS；本机服务可使用 HTTP')
    if parsed.path.endswith(('/chat/completions','/responses','/models')):
        raise AIGatewayError('请填写服务基地址，不要包含完整请求端点')
    return value

def _defaults():
    return {'version':1,'active_provider':'deepseek','profiles':{
        name:{'base_url':p['base_url'],'model':p['model'],'api_key':'','env_key_allowed':True}
        for name,p in PROVIDERS.items()}}

def _validated_profile(provider,value,previous=None):
    _provider_id(provider)
    if not isinstance(value,dict):
        raise AIGatewayError('供应商配置格式无效')
    definition=PROVIDERS[provider]
    previous=previous or {'base_url':definition['base_url'],'model':definition['model'],'api_key':'','env_key_allowed':True}
    base=_base_url(value.get('base_url',previous['base_url']),allow_empty=provider=='custom')
    if provider!='custom' and base!=definition['base_url']:
        raise AIGatewayError('内置供应商使用固定官方地址，不能修改或转发到其他服务')
    model=_model(value.get('model',previous['model']),allow_empty=provider=='custom')
    supplied=_key(value.get('api_key',''))
    retained=previous.get('api_key','') if base==previous['base_url'] else ''
    env_allowed=bool(previous.get('env_key_allowed',True))
    if provider=='custom' and previous['base_url'] and base!=previous['base_url']:
        env_allowed=False
    return {'base_url':base,'model':model,'api_key':_key(supplied or retained),'env_key_allowed':env_allowed}

@contextmanager
def _locked_profiles():
    if not _THREAD_LOCK.acquire(timeout=LOCK_TIMEOUT):
        raise AIGatewayError('AI 配置正在被另一任务更新，请稍后重试')
    handle=None
    locked=False
    try:
        folder=Path(credential_dir())
        folder.mkdir(parents=True,exist_ok=True)
        descriptor=os.open(folder/'ai-profiles.lock',os.O_RDWR|os.O_CREAT,0o600)
        handle=os.fdopen(descriptor,'r+b')
        if os.fstat(handle.fileno()).st_size==0:
            handle.write(b'\0')
            handle.flush()
        deadline=time.monotonic()+LOCK_TIMEOUT
        while True:
            try:
                if os.name=='nt':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                locked=True
                break
            except OSError:
                if time.monotonic()>=deadline:
                    raise AIGatewayError('AI 配置正在被另一程序更新，请稍后重试') from None
                time.sleep(.025)
        yield folder
    except OSError:
        raise AIGatewayError('无法读取或保存用户级 AI 配置，请检查目录权限') from None
    finally:
        try:
            if handle is not None:
                if locked:
                    try:
                        if os.name=='nt':
                            import msvcrt
                            handle.seek(0)
                            msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(),fcntl.LOCK_UN)
                    except OSError:
                        pass
                handle.close()
        finally:
            _THREAD_LOCK.release()

def _read_json(path):
    try:
        with path.open('rb') as source:
            data=source.read(MAX_PROFILE_BYTES+1)
        if len(data)>MAX_PROFILE_BYTES:
            raise AIGatewayError('AI 配置文件超过允许大小')
        value=json.loads(data.decode('utf-8-sig'))
        if not isinstance(value,dict):
            raise AIGatewayError('AI 配置文件格式无效；原文件未被覆盖')
        return value
    except (UnicodeError,json.JSONDecodeError):
        raise AIGatewayError('AI 配置文件格式无效；原文件未被覆盖') from None

def _write_locked(folder,state):
    temporary=None
    try:
        descriptor,temporary=tempfile.mkstemp(prefix='.ai-profiles-',suffix='.tmp',dir=folder)
        with os.fdopen(descriptor,'w',encoding='utf-8') as target:
            json.dump(state,target,ensure_ascii=False,indent=2,allow_nan=False)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary,folder/'ai-profiles.json')
        temporary=None
        if os.name!='nt':
            (folder/'ai-profiles.json').chmod(0o600)
    finally:
        if temporary is not None:
            try: Path(temporary).unlink()
            except OSError: pass

def _load_locked(folder):
    state=_defaults()
    path=folder/'ai-profiles.json'
    if path.exists():
        loaded=_read_json(path)
        if loaded.get('version',1)!=1 or not isinstance(loaded.get('profiles'),dict):
            raise AIGatewayError('AI 配置版本或结构无效；原文件未被覆盖')
        state['active_provider']=_provider_id(loaded.get('active_provider','deepseek'))
        for provider,profile in loaded['profiles'].items():
            _provider_id(provider)
            state['profiles'][provider]=_validated_profile(provider,profile)
            if profile.get('env_key_allowed') is False:
                state['profiles'][provider]['env_key_allowed']=False
        return state
    legacy=folder/'auction-lab-llm.json'
    if legacy.exists():
        profile=_validated_profile('custom',_read_json(legacy))
        if profile['base_url'] and profile['model']:
            state['profiles']['custom']=profile
            state['active_provider']='custom'
    _write_locked(folder,state)
    return state

def _effective_config(state,provider):
    definition=PROVIDERS[provider]
    profile=state['profiles'][provider]
    stored=profile.get('api_key','')
    environmental=os.environ.get(definition['env'],'') if not stored and profile.get('env_key_allowed',True) else ''
    key=_key(stored or environmental)
    return {'provider':provider,'label':definition['label'],'base_url':profile['base_url'],
            'model':profile['model'],'protocol':definition['protocol'],'api_key':key,
            'key_source':'saved' if stored else 'environment' if environmental else 'missing'}

def _public_state(state):
    profiles=[]
    for provider,definition in PROVIDERS.items():
        config=_effective_config(state,provider)
        has_key=bool(config['api_key'])
        profiles.append({'id':provider,'label':config['label'],'base_url':config['base_url'],'model':config['model'],
                         'protocol':config['protocol'],'has_key':has_key,
                         'configured':bool(config['base_url'] and config['model'] and (has_key or provider=='custom')),
                         'models':list(definition['models']),'key_source':config['key_source']})
    return {'active_provider':state['active_provider'],'profiles':profiles}

def profiles_state():
    with _locked_profiles() as folder:
        return _public_state(_load_locked(folder))

def active_config(provider=None):
    with _locked_profiles() as folder:
        state=_load_locked(folder)
        selected=_provider_id(provider) if provider is not None else state['active_provider']
        return _effective_config(state,selected)

def save_profile(value):
    if not isinstance(value,dict):
        raise AIGatewayError('供应商配置必须为对象')
    provider=_provider_id(value.get('provider'))
    with _locked_profiles() as folder:
        state=_load_locked(folder)
        state['profiles'][provider]=_validated_profile(provider,value,state['profiles'][provider])
        _write_locked(folder,state)
        return _public_state(state)

def select_provider(provider):
    provider=_provider_id(provider)
    with _locked_profiles() as folder:
        state=_load_locked(folder)
        state['active_provider']=provider
        _write_locked(folder,state)
        return _public_state(state)

def _runtime_config(config):
    if not isinstance(config,dict):
        raise AIGatewayError('请先配置并选择模型服务')
    provider=_provider_id(config.get('provider','custom'))
    profile=_validated_profile(provider,config)
    protocol=PROVIDERS[provider]['protocol']
    if config.get('protocol',protocol)!=protocol:
        raise AIGatewayError('供应商协议与当前配置不一致')
    if not profile['base_url'] or not profile['model']:
        raise AIGatewayError('请先填写模型服务基地址和模型名称')
    if provider!='custom' and not profile['api_key']:
        raise AIGatewayError('请先保存当前供应商的 API Key 或配置其环境变量')
    return {**profile,'provider':provider,'protocol':protocol,'label':PROVIDERS[provider]['label']}

def _http_message(code):
    messages={400:'模型 API 参数被拒绝，请核对模型和协议',401:'模型 API 认证失败，请核对当前供应商的 Key',
              402:'模型 API 余额或计费状态不足',403:'模型 API 无访问权限，请核对账户与模型权限',
              404:'模型或接口不存在，请核对模型名称与服务地址',413:'模型 API 拒绝过大的输入',
              429:'模型 API 限流或额度不足，请稍后手动重试'}
    return messages.get(code,'模型服务暂时不可用，请稍后手动重试')+(f'（HTTP {code}）' if isinstance(code,int) else '')

def _request_json(config,suffix,payload=None,timeout=90):
    if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or not 1<=timeout<=180:
        raise AIGatewayError('请求超时设置须在 1–180 秒之间')
    body=None
    if payload is not None:
        try: body=json.dumps(payload,ensure_ascii=False,allow_nan=False).encode('utf-8')
        except (TypeError,ValueError,UnicodeError): raise AIGatewayError('模型输入格式无效') from None
        if len(body)>MAX_INPUT_BYTES:
            raise AIGatewayError('模型输入超过大小限制，请缩短问题或历史对话')
    headers={'Accept':'application/json','User-Agent':'AuctionLab-AI/1.2'}
    if body is not None: headers['Content-Type']='application/json'
    if config['api_key']: headers['Authorization']='Bearer '+config['api_key']
    request=Request(config['base_url']+suffix,data=body,headers=headers,method='POST' if body is not None else 'GET')
    try:
        with build_opener(NoRedirect()).open(request,timeout=timeout) as response:
            status=getattr(response,'status',200)
            if status!=200: raise AIGatewayError(_http_message(status))
            raw=response.read(MAX_RESPONSE_BYTES+1)
        if len(raw)>MAX_RESPONSE_BYTES:
            raise AIGatewayError('模型响应超过允许大小，请缩短输出后重试')
        result=json.loads(raw.decode('utf-8-sig'),parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    except HTTPError as exc:
        status=exc.code
        exc.close()
        raise AIGatewayError(_http_message(status)) from None
    except (URLError,TimeoutError,OSError):
        raise AIGatewayError('模型 API 网络连接失败或超时；未自动重试，请确认状态后再操作') from None
    except AIGatewayError: raise
    except (ValueError,UnicodeError):
        raise AIGatewayError('模型 API 返回无效 JSON，未读取为分析结果') from None
    if not isinstance(result,dict): raise AIGatewayError('模型响应格式无效')
    if result.get('error') is not None:
        raise AIGatewayError('模型服务返回错误状态，请核对当前供应商的授权与模型')
    return result

def test_connection(provider=None):
    selected=provider if isinstance(provider,str) and provider in PROVIDERS else None
    model=None
    try:
        raw_config=active_config(provider)
        selected,model=raw_config['provider'],raw_config['model']
        config=_runtime_config(raw_config)
        result=_request_json(config,'/models',timeout=20)
        data=result.get('data')
        if not isinstance(data,list): raise AIGatewayError('模型列表响应格式无效；未发送生成请求')
        models=sorted({row['id'] for row in data if isinstance(row,dict) and isinstance(row.get('id'),str) and _MODEL_ID.fullmatch(row['id'])})
        available=model in models
        return {'ok':True,'provider':selected,'model':model,'model_available':available,'models':models,
                'message':'连接成功，当前模型在可见列表中；未发送付费生成请求' if available else '连接成功，但列表未包含当前模型；请核对名称与权限，未发送付费生成请求'}
    except AIGatewayError as exc:
        return {'ok':False,'provider':selected,'model':model,'model_available':None,'message':str(exc)}

def _messages(messages):
    if not isinstance(messages,list) or not 1<=len(messages)<=MAX_MESSAGES:
        raise AIGatewayError('对话须包含 1–100 条文本消息')
    clean=[]
    size=0
    for message in messages:
        if not isinstance(message,dict) or message.get('role') not in ('system','developer','user','assistant') or not isinstance(message.get('content'),str):
            raise AIGatewayError('对话仅支持 system、developer、user、assistant 的纯文本消息')
        size+=len(message['content'])
        if size>MAX_INPUT_CHARS: raise AIGatewayError('对话内容超过 200000 字符，请缩短历史记录')
        clean.append({'role':message['role'],'content':message['content']})
    if not any(item['content'].strip() for item in clean): raise AIGatewayError('对话内容不能为空')
    return clean

def _usage(value):
    allowed={'input_tokens','output_tokens','total_tokens','prompt_tokens','completion_tokens',
             'prompt_cache_hit_tokens','prompt_cache_miss_tokens','cached_tokens','reasoning_tokens'}
    nested={'input_tokens_details','output_tokens_details','prompt_tokens_details','completion_tokens_details'}
    if not isinstance(value,dict): return {}
    result={}
    for key,amount in value.items():
        if key in allowed and isinstance(amount,int) and not isinstance(amount,bool) and amount>=0: result[key]=amount
        elif key in nested and isinstance(amount,dict): result[key]=_usage(amount)
    return result

def _responses_text(result):
    status=result.get('status')
    if status not in ('completed','incomplete'):
        raise AIGatewayError('模型非流式请求未正常完成，未把中间状态当作答案')
    output=result.get('output')
    if not isinstance(output,list): raise AIGatewayError('模型 Responses 输出格式无效')
    texts=[]
    refused=False
    for item in output:
        if not isinstance(item,dict) or item.get('type')!='message': continue
        content=item.get('content',[])
        if not isinstance(content,list): continue
        for part in content:
            if not isinstance(part,dict): continue
            if part.get('type')=='output_text' and isinstance(part.get('text'),str): texts.append(part['text'])
            elif part.get('type')=='refusal': refused=True
    warnings=[]
    finish='stop'
    if status=='incomplete':
        details=result.get('incomplete_details') or {}
        reason=details.get('reason') if isinstance(details,dict) else None
        finish='length' if reason=='max_output_tokens' else 'incomplete'
        warnings.append('模型输出未完整结束，当前文字仅为部分结果；未自动续费续写')
    if refused:
        warnings.append('模型拒绝了全部或部分请求')
        finish='refusal'
    return '\n'.join(texts).strip(),warnings,finish

def _chat_text(result):
    choices=result.get('choices')
    if not isinstance(choices,list) or not choices or not isinstance(choices[0],dict):
        raise AIGatewayError('模型 Chat Completions 响应格式无效')
    choice=choices[0]
    message=choice.get('message')
    if not isinstance(message,dict): raise AIGatewayError('模型返回的消息格式无效')
    text=message.get('content')
    text=text.strip() if isinstance(text,str) else ''
    reason=choice.get('finish_reason')
    if reason not in ('stop','length','content_filter','tool_calls','function_call','insufficient_system_resource'):
        raise AIGatewayError('模型未提供可确认的结束状态，未把中间结果当作答案')
    warnings=[]
    if reason=='length': warnings.append('模型输出达到长度限制，当前文字仅为部分结果；未自动续费续写')
    elif reason=='insufficient_system_resource': warnings.append('模型服务资源不足，当前文字可能不完整；未自动重试')
    elif reason in ('tool_calls','function_call'): warnings.append('模型返回工具调用，但本客户端不会执行工具；当前文字可能不完整')
    if message.get('refusal') or reason=='content_filter':
        warnings.append('模型拒绝了全部或部分请求')
        reason='refusal'
    return text,warnings,reason

def complete(config,messages,timeout=90):
    config=_runtime_config(copy.deepcopy(config))
    messages=_messages(messages)
    if config['protocol']=='responses':
        payload={'model':config['model'],'input':messages,'store':False,'stream':False,'max_output_tokens':4096}
        result=_request_json(config,'/responses',payload=payload,timeout=timeout)
        text,warnings,finish=_responses_text(result)
    else:
        messages=[{'role':'system' if item['role']=='developer' else item['role'],'content':item['content']} for item in messages]
        payload={'model':config['model'],'messages':messages,'stream':False,'max_tokens':4096}
        if config['provider']=='deepseek': payload['thinking']={'type':'disabled'}
        result=_request_json(config,'/chat/completions',payload=payload,timeout=timeout)
        text,warnings,finish=_chat_text(result)
    if not text:
        if finish=='refusal': raise AIGatewayError('模型拒绝了本次请求，未返回可显示正文')
        if finish in ('length','incomplete'):
            raise AIGatewayError('模型在完成正文前达到限制或中断；未返回可显示文本，未自动续写')
        raise AIGatewayError('模型未返回可显示的文本；未把推理或工具内容当作答案')
    return {'text':text,'provider':config['provider'],'model':config['model'],'usage':_usage(result.get('usage')),
            'warnings':warnings,'finish_reason':finish}
