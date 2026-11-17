"""Pinned-block contract structure and bounded, ABI-backed read-only probes."""
from copy import deepcopy
import hashlib
import json
import re
import time
from eth_abi import decode,encode
from eth_utils.abi import collapse_if_tuple
from web3 import Web3
from .abi import canonical,encode_call,resolve,decode_revert
from .common import ToolError,address,hexdata,jsonable,require,uint,txhash
from .explorers import Sources
from .cache import SnapshotCache
from .network import Budget
from .materials import material

IMPL=hex(int.from_bytes(Web3.keccak(text='eip1967.proxy.implementation'),'big')-1)
BEACON=hex(int.from_bytes(Web3.keccak(text='eip1967.proxy.beacon'),'big')-1)
LEGACY=Web3.to_hex(Web3.keccak(text='PROXIABLE'))
ZERO='0x'+'00'*20
RELATIONS=('core','validator','underlying','asset','grvDistributor','rebateDistributor','rateModel','owner','admin','keeper','priceCalculator','implementation','target','beacon')
PRIORITY=('name','symbol','decimals','totalSupply','core','underlying','asset','validator','paused','getCash','exchangeRate','totalAssets','owner','admin','balanceOf','allowance')
LIMITS={'time_budget_seconds':(60,1,600),'max_requests':(150,5,5000),'max_probes':(50,1,500),'max_nodes':(10,1,100),'max_proxy_depth':(4,1,16),'max_source_files':(1,0,20)}


def options(params):
    out={}
    for key,(default,low,high) in LIMITS.items():
        value=uint(params.get(key,default),key);require(low<=value<=high,f'{key} must be {low}..{high}');out[key]=value
    return out


def hexnum(value):
    return int(value,16) if isinstance(value,str) and value.startswith('0x') else int(value)


class Inspector:
    def __init__(self,config,chain,rpc,params):
        self.config,self.chain,self.rpc,self.params=config,chain,rpc,params
        self.limits=options(params);self.budget=Budget(self.limits['time_budget_seconds'],self.limits['max_requests'])
        self.sources=Sources(config,chain,self.budget,enabled=params.get('external',True),no_cache=params.get('no_cache',False))
        self.cache=SnapshotCache(config);self.nodes={};self.edges=[];self.issues=[];self.structures={};self.materials={};self.memo={};self.probes={};self.omitted_probes=0;self.files_used=0
        self.account=address(params['account']) if params.get('account') else None
        tag=params.get('block','latest')
        require(tag!='pending','Contract diagnostics need a mined block, not pending')
        if tag not in ('latest','safe','finalized','earliest'):tag=hex(uint(tag,'block'))
        block=self.query('eth_getBlockByNumber',[tag,False])
        require(isinstance(block,dict) and block.get('number') is not None and block.get('hash'),'Cannot pin requested block','RPC_UNAVAILABLE')
        self.block=hex(uint(hexnum(block['number']),'block number'));self.block_hash=txhash(block['hash']);self.checked_at=time.time();self.canonicality='unchecked'
        self.budget.reserve_requests=1

    def query(self,method,params):return self.rpc.optional_request(method,params,self.budget)

    def issue(self,capability,reason,where=None):
        row={'capability':capability,'source':'rpc' if capability in ('structure','probes','canonicality') else 'analysis','reason':reason}
        if where:row['address']=where
        if row not in self.issues:self.issues.append(row)

    def code(self,target,latest=False):
        target=address(target);key=('code',target,'latest' if latest else self.block)
        if key not in self.memo:self.memo[key]=hexdata(self.query('eth_getCode',[target,key[-1]]))
        return self.memo[key]

    def node(self,target,role='relation'):
        target=address(target)
        if target in self.nodes:
            if role not in self.nodes[target]['roles']:self.nodes[target]['roles'].append(role)
            return self.nodes[target]
        if len(self.nodes)>=self.limits['max_nodes']:
            self.issue('relations','truncated',target);return None
        out={'address':target,'roles':[role],'is_contract':None,'verified':None,'name':None,'code_hash':None}
        self.nodes[target]=out
        try:
            code=self.code(target);out.update(is_contract=code!='0x',code_hash=Web3.to_hex(Web3.keccak(hexstr=code)),code_bytes=(len(code)-2)//2)
        except ToolError as exc:self.issue('structure',exc.code,target)
        return out

    def get_material(self,target,source=False):
        target=address(target);key=(target,source)
        if key in self.materials:return self.materials[key]
        node=self.node(target)
        empty={'abi':None,'verified':None,'name':None,'files':{},'main_path':None,'source_status':'unavailable','metadata':{},'provider':None}
        if not node or not node['is_contract']:return empty
        try:
            # Explorer records describe current deployments; bind them to the pinned code.
            if self.code(target,latest=True)!=self.code(target):
                self.issue('abi','history_code_mismatch',target);self.materials[key]=empty;return empty
            out=material(self.sources,target,node['code_hash'],include_source=source)
            node.update(verified=out['verified'],name=out['name'])
            self.materials[key]=out;return out
        except ToolError as exc:
            self.issue('source' if source else 'abi',exc.code,target);self.materials[key]=empty;return empty

    def _pointer(self,storage,slot):
        raw=hexdata(self.query('eth_getStorageAt',[storage,slot,self.block]));require(len(raw)==66,'Invalid proxy slot','INVALID_RESPONSE')
        if int(raw,16)==0:return None
        require(int(raw[2:26],16)==0,'Non-address proxy slot','INVALID_RESPONSE')
        return address('0x'+raw[-40:])

    def structure(self,root):
        root=address(root)
        if root in self.structures:return self.structures[root]
        result={'address':root,'kind':'contract','is_proxy':False,'proxy_type':None,'layers':[],
                'resolution_status':'complete','effective_address':None,'effective_abi_source':None,'depth_limited':False,'candidates':[]}
        self.structures[root]=result
        current=root;seen=set()
        for depth in range(self.limits['max_proxy_depth']+1):
            if current in seen:
                result['resolution_status']='partial';self.issue('structure','PROXY_CYCLE',current);break
            seen.add(current);node=self.node(current,'root' if depth==0 else 'implementation')
            if not node or node['is_contract'] is not True:
                result['resolution_status']='partial';self.issue('structure','IMPLEMENTATION_NO_CODE' if depth else 'CODE_UNAVAILABLE',current);break
            info=self.get_material(current);code=self.code(current)
            layer={'depth':depth,'address':current,'code_address':current,'storage_address':root,'call_address':root,'name':info['name'],
                   'role':'contract' if depth==0 else 'implementation','verified':info['verified'],'abi_source':info['provider'],'code_hash':node['code_hash'],'block':self.block}
            result['layers'].append(layer)
            pointer=None;kind=None;signal=None
            minimal=re.fullmatch(r'0x363d3d373d3d3d363d73([0-9a-f]{40})5af43d82803e903d91602b57fd5bf3',code)
            if minimal:pointer=address('0x'+minimal[1]);kind='eip1167';signal='exact_runtime_pattern'
            elif len(code)==48 and code.startswith('0xef0100'):pointer=address('0x'+code[8:]);kind='eip7702';signal='delegation_marker'
            elif depth==0:
                try:
                    pointer=self._pointer(root,IMPL)
                    if pointer:kind='eip1967';signal='implementation_slot'
                    else:
                        beacon=self._pointer(root,BEACON)
                        if beacon:
                            bn=self.node(beacon,'beacon')
                            require(bn and bn['is_contract'],'Beacon has no code','INVALID_RESPONSE')
                            raw=self.query('eth_call',[{'to':beacon,'data':encode_call('implementation()',[])},self.block])
                            value=decode(['address'],bytes.fromhex(hexdata(raw)[2:]))[0];pointer=address(value)
                            self.edges.append({'from':root,'to':beacon,'name':'beacon','evidence':'eip1967_beacon_slot'})
                            kind='eip1967_beacon';signal='beacon_slot_and_getter'
                        else:
                            pointer=self._pointer(root,LEGACY)
                            if pointer:kind='eip1822';signal='proxiable_slot'
                except (ToolError,ValueError) as exc:
                    self.issue('structure',exc.code if isinstance(exc,ToolError) else 'invalid_pointer',root);result['resolution_status']='partial'
            elif (info.get('metadata') or {}).get('is_proxy'):
                result['resolution_status']='partial';self.issue('structure','PROXY_CONTEXT_UNRESOLVED',current)
                result['candidates']=(info['metadata'].get('implementations') or [])[:10]
                break
            if pointer is not None:
                result['is_proxy']=True
                if depth==0 and kind=='eip7702':result['kind']='delegated_account'
                if result['proxy_type'] is None:result['proxy_type']=kind
                layer['role']='proxy';layer['pointer_evidence']=signal;layer['management_abi']=info.get('abi')
                self.edges.append({'from':current,'to':pointer,'name':'implementation','evidence':signal,'storage_address':root})
                if depth==self.limits['max_proxy_depth']:
                    result.update(depth_limited=True,resolution_status='partial');self.issue('structure','PROXY_DEPTH_EXCEEDED',root);break
                current=pointer;continue
            reported=(info.get('metadata') or {}).get('implementations') or []
            if depth==0 and reported:
                result.update(resolution_status='partial',candidates=reported[:10]);self.issue('structure','REPORTED_PROXY_UNCONFIRMED',root);break
            if result['resolution_status']=='complete':
                result['effective_address']=current;result['effective_abi_source']='implementation' if depth else 'self'
            break
        return result

    def abi(self,target):
        s=self.structure(target);effective=s['effective_address']
        if not effective:return None
        info=self.get_material(effective)
        if info['verified'] is not True or not isinstance(info['abi'],list):
            self.issue('abi','IMPLEMENTATION_UNVERIFIED' if info['verified'] is False else 'not_available',effective);return None
        return info['abi']

    def probe_key(self,target,signature,args):return (address(target),signature,json.dumps(jsonable(args),sort_keys=True),self.account)

    def prepare(self,target,signature,args):
        target=address(target);abi=self.abi(target)
        require(abi is not None,'No verified effective ABI at the pinned block','ABI_UNAVAILABLE')
        item=resolve(abi,signature)
        require(item.get('stateMutability') in ('view','pure'),'Automatic probes may call view/pure functions only','READ_ONLY_REQUIRED')
        signature=canonical(item);key=self.probe_key(target,signature,args)
        if key in self.probes:return key,None
        if len(self.probes)>=self.limits['max_probes']:
            self.omitted_probes+=1;raise ToolError('PROBE_LIMIT','State probe limit reached')
        tx={'to':target,'data':encode_call(signature,args)}
        if self.account:tx['from']=self.account
        row={'address':target,'function':signature,'args':args,'status':'pending','block':self.block,'caller':self.account,'source':'verified_abi','item':item}
        self.probes[key]=row
        return key,('eth_call',[tx,self.block])

    def finish_probe(self,key,response):
        row=self.probes[key];item=row.pop('item',None)
        if not item:return row
        if response.get('error'):
            code=response['error'];row.update(status='reverted' if response.get('rpc_error_kind')=='reverted' else 'timeout' if code=='SOURCE_TIMEOUT' else 'unavailable' if code in ('ANALYSIS_LIMIT','PROBE_LIMIT') else 'rejected',reason=code)
            if response.get('revert_data'):row.update(decode_revert(response['revert_data']))
            self.issue('probes',row['status'],row['address']);return row
        try:
            raw=hexdata(response['result']);row['raw']=raw
            require(len(raw)<=2*1024*1024+2,'Probe response too large','DECODE_ERROR')
            types=[collapse_if_tuple(i) for i in item.get('outputs',[])]
            values=decode(types,bytes.fromhex(raw[2:]));require(encode(types,values).hex()==raw[2:],'Noncanonical probe response','DECODE_ERROR')
            names=[i.get('name') or f'out{j}' for j,i in enumerate(item.get('outputs',[]))]
            if len(set(names))!=len(names):names=[f'out{j}' for j in range(len(values))]
            row.update(status='success',values=jsonable(values),named_values=dict(zip(names,jsonable(values))),types=types)
        except Exception:row.update(status='decode_failed');self.issue('probes','decode_failed',row['address'])
        return row

    def read(self,target,signature,args=None):
        args=[] if args is None else args
        try:
            key,request=self.prepare(target,signature,args)
            if request is not None:
                try:response={'result':self.query(*request)}
                except ToolError as exc:response={'error':exc.code,**(exc.data if isinstance(exc.data,dict) else {})}
                return self.finish_probe(key,response)
            return self.probes[key]
        except ToolError as exc:
            self.issue('probes',exc.code,address(target));return {'address':address(target),'function':signature,'status':'unknown','reason':exc.code}

    def batch_probes(self,target):
        abi=self.abi(target)
        requested=self.params.get('functions')
        require(requested is None or isinstance(requested,list) and len(requested)<=1000,'functions must be a JSON array with at most 1000 entries')
        if abi is None:
            rows=[]
            for spec in (requested or [])[:self.limits['max_probes']]:
                name=spec if isinstance(spec,str) else spec.get('function') if isinstance(spec,dict) else None
                require(isinstance(name,str) and re.fullmatch(r'[A-Za-z_]\w*(?:\(.*\))?',name) is not None,'Invalid function specification')
                rows.append({'address':target,'function':name,'status':'unavailable','reason':'ABI_UNAVAILABLE'})
            return rows
        auto=self.params.get('auto',requested is None)
        require(isinstance(auto,bool),'auto must be boolean')
        specs=[]
        if auto:
            fns=[i for i in abi if i.get('type')=='function' and i.get('stateMutability') in ('view','pure') and i.get('name') not in ('tokenURI','uri')]
            fns.sort(key=lambda i:(PRIORITY.index(i['name']) if i['name'] in PRIORITY else len(PRIORITY),canonical(i)))
            for item in fns:
                fields=item.get('inputs',[]);sig=canonical(item)
                if not fields:specs.append((sig,[]))
                elif self.account and sig in ('balanceOf(address)','maxRedeem(address)','maxWithdraw(address)'):specs.append((sig,[self.account]))
                else:specs.append((sig,None))
        for spec in requested or []:
            require(isinstance(spec,(str,dict)),'Each function is a signature or {function,args}')
            if isinstance(spec,dict):require(not set(spec)-{'function','args'} and 'function' in spec,'Invalid function specification')
            name=spec if isinstance(spec,str) else spec['function'];item=resolve(abi,name)
            require(item.get('stateMutability') in ('view','pure'),'Explicit probes may not call mutating functions','READ_ONLY_REQUIRED')
            args=[] if isinstance(spec,str) else spec.get('args',[]);specs.append((canonical(item),args))
        requests=[];selected=[];skipped=[];seen=set()
        for sig,args in specs:
            identity=(sig,json.dumps(jsonable(args),sort_keys=True))
            if identity in seen:continue
            seen.add(identity)
            if len(selected)+len(skipped)>=self.limits['max_probes']:self.omitted_probes+=1;continue
            if args is None:skipped.append({'address':target,'function':sig,'status':'skipped_requires_arguments'});continue
            try:
                key,request=self.prepare(target,sig,args);selected.append(key)
                if request:requests.append((key,request))
            except ToolError as exc:
                self.issue('probes',exc.code,target)
                skipped.append({'address':target,'function':sig,'status':'invalid_arguments' if exc.code=='INVALID_INPUT' else 'unavailable','reason':exc.code})
        for start in range(0,len(requests),20):
            chunk=requests[start:start+20]
            try:responses=self.rpc.optional_batch([r for _,r in chunk],self.budget)
            except ToolError as exc:responses=[{'error':exc.code} for _ in chunk]
            if not isinstance(responses,list) or len(responses)!=len(chunk):responses=[{'error':'INVALID_RESPONSE'} for _ in chunk]
            retryable_batch={'OPTIONAL_RPC_REJECTED','CAPABILITY_UNAVAILABLE','INVALID_RESPONSE'}
            for index,(_,request) in enumerate(chunk):
                if responses[index].get('error') in retryable_batch and responses[index].get('rpc_error_kind')!='reverted':
                    try:responses[index]={'result':self.query(*request)}
                    except ToolError as exc:responses[index]={'error':exc.code,**(exc.data if isinstance(exc.data,dict) else {})}
            for (key,_),response in zip(chunk,responses):self.finish_probe(key,response)
        return skipped

    def relation(self,target,name):
        abi=self.abi(target)
        if abi is None:return None
        matches=[i for i in abi if i.get('type')=='function' and i.get('name')==name and not i.get('inputs') and i.get('stateMutability') in ('view','pure') and [o.get('type') for o in i.get('outputs',[])]==['address']]
        if len(matches)!=1:return None
        row=self.read(target,canonical(matches[0]))
        if row.get('status')!='success':return None
        value=address(row['values'][0])
        if value==ZERO:return None
        node=self.node(value,name)
        if not node:return None
        edge={'from':address(target),'to':value,'name':name,'evidence':{'function':row['function'],'block':self.block,'raw':row.get('raw')}}
        if edge not in self.edges:self.edges.append(edge)
        if node['is_contract']:self.get_material(value)
        return value

    def semantics(self,target):
        from .semantics import extract,VERSION
        structure=self.structure(target);effective=structure['effective_address']
        if self.params.get('no_semantics') or not effective:return None
        if self.files_used>=self.limits['max_source_files']:
            self.issue('semantics','source_file_limit',effective);return None
        info=self.get_material(effective,source=True)
        if info.get('verified') is not True or info.get('source_status')!='available':self.issue('semantics','source_unavailable',effective);return None
        paths=[info['main_path']]+sorted(p for p in info['files'] if p!=info['main_path'])
        available=self.limits['max_source_files']-self.files_used
        parsed_files=[]
        for path in paths[:available]:
            source=info['files'][path];self.files_used+=1
            key=hashlib.sha256(json.dumps([VERSION,self.chain['chain_id'],effective,self.nodes[effective]['code_hash'],source['sha256'],path,info.get('source_provider'),info.get('abi')],sort_keys=True).encode()).hexdigest()
            parsed=None if self.params.get('no_cache') else self.cache.get(key)
            if parsed is None:
                parsed=extract(source['content'],path,info['abi']);self.cache.put(key,parsed,3600)
            if self.params.get('include_source'):parsed={**parsed,'source_text':source['content']}
            parsed_files.append(parsed)
        first=parsed_files[0]
        return {**first,'functions':[f for r in parsed_files for f in r['functions']],'modifiers':[m for r in parsed_files for m in r['modifiers']],
                'status':'partial' if any(r['semantics_partial'] for r in parsed_files) else 'extracted','semantics_partial':any(r['semantics_partial'] for r in parsed_files),
                'files':[{'file':r['file'],'source_sha256':r['source_sha256'],'status':r['status'],**({'source_text':r['source_text']} if 'source_text' in r else {})} for r in parsed_files],
                'files_omitted':len(paths)-len(parsed_files)}

    def canonical_check(self):
        self.budget.reserve_requests=0
        try:
            block=self.query('eth_getBlockByNumber',[self.block,False]);self.canonicality='verified' if isinstance(block,dict) and block.get('hash')==self.block_hash else 'reorged'
        except ToolError:self.canonicality='unchecked'
        if self.canonicality!='verified':self.issue('canonicality',self.canonicality)

    def report(self,root,skipped=None,semantics=None):
        abi=self.abi(root);s=self.structure(root);effective=s['effective_address'];mat=self.get_material(effective) if effective else {}
        all_probes=[{k:v for k,v in row.items() if k!='item'} for row in self.probes.values()]+(skipped or [])
        if self.omitted_probes:self.issue('probes','truncated',root)
        return {'structure':s,'abi':{'source':mat.get('provider'),'address':effective,'verified':mat.get('verified'),'items':abi,
                    'functions_count':sum(i.get('type')=='function' for i in abi or []),'events_count':sum(i.get('type')=='event' for i in abi or []),'errors_count':sum(i.get('type')=='error' for i in abi or [])} if abi is not None else None,
                'probes':all_probes,'relations':{'nodes':list(self.nodes.values()),'edges':self.edges,'scope':'one_hop_plus_pattern_dependencies','truncated':any(i['capability']=='relations' and i['reason']=='truncated' for i in self.issues)},
                'semantics':semantics,'patterns':[],'issues':self.issues,'enhancements_missing':self.sources.enhancements_missing,
                'analysis_status':'complete' if s['resolution_status']=='complete' and abi is not None and self.canonicality=='verified' else 'partial',
                'components':{'structure':s['resolution_status'],'abi':'available' if abi is not None else 'unavailable','probes':'partial' if self.omitted_probes or any(r['status'] not in ('success','skipped_requires_arguments') for r in all_probes) else 'complete','semantics':semantics.get('status') if semantics else 'unavailable'},
                'block':{'number':str(hexnum(self.block)),'hash':self.block_hash,'canonicality':self.canonicality},'checked_at':self.checked_at,
                'capabilities':{'requests_used':self.budget.requests,'rpc_calls':self.budget.rpc_calls,'requests_by_stage':self.budget.by_stage,'limits':self.limits,'omitted_probes':self.omitted_probes}}


def inspect_contract(config,chain,rpc,params,diagnose=False):
    require(params.get('view','summary') in ('summary','full'),'view must be summary or full')
    require(not (params.get('include_source') and params.get('view','summary')=='summary'),'include_source requires view=full')
    target=address(params['address'])
    try:inspector=Inspector(config,chain,rpc,params)
    except ToolError as exc:
        if exc.code=='INVALID_INPUT':raise
        raise ToolError('CHAIN_ID_MISMATCH' if exc.code=='CHAIN_ID_MISMATCH' else 'RPC_UNAVAILABLE','Cannot establish a pinned contract-analysis block',retryable=exc.code!='CHAIN_ID_MISMATCH',data={'structure':{'address':target,'kind':'unknown','resolution_status':'partial'},'verdict':{'answer':'unknown','entry':None} if diagnose else None,'issues':[{'capability':'structure','source':'rpc','reason':exc.code}]}) from None
    node=inspector.node(target,'root')
    if node and node['is_contract'] is False:
        raise ToolError('NOT_A_CONTRACT','Address has no deployed contract code at the pinned block',data={'structure':{'address':target,'kind':'precompile' if int(target,16)<=0x11 else 'empty_code','code_bytes':0},'block':{'number':str(hexnum(inspector.block)),'hash':inspector.block_hash}})
    if node is None or node['is_contract'] is None:raise ToolError('RPC_UNAVAILABLE','Cannot establish root contract code',retryable=True,data={'structure':{'address':target,'kind':'unknown'},'verdict':{'answer':'unknown'} if diagnose else None})
    inspector.structure(target);skipped=[];sem=None;diagnostic=None
    try:
        if diagnose:
            for name in ('core','asset','underlying'):inspector.relation(target,name)
            sem=inspector.semantics(target)
            from .diagnose import diagnose_contract
            diagnostic=diagnose_contract(inspector,target,sem)
        skipped=inspector.batch_probes(target)
        for name in RELATIONS:
            if len(inspector.nodes)>=inspector.limits['max_nodes']:inspector.issue('relations','truncated',target);break
            inspector.relation(target,name)
        if not diagnose:sem=inspector.semantics(target)
    except ToolError as exc:
        if exc.code in ('INVALID_INPUT','READ_ONLY_REQUIRED'):raise
        inspector.issue('orchestration',exc.code,target)
    inspector.canonical_check();out=inspector.report(target,skipped,sem)
    if diagnose:
        out.update(diagnostic or {'patterns':[],'verdict':{'question':'can_redeem','answer':'unknown','confidence':'low','entry':None,'checks':[],'blockers':[],'unknowns':[{'id':'incomplete','severity':'blocker','reason':'diagnostic_incomplete'}]}})
        if inspector.canonicality!='verified':
            out['verdict'].update(answer='unknown',confidence='low');out['verdict']['unknowns'].append({'id':'canonicality','severity':'blocker','reason':inspector.canonicality})
    return out


def discover_abi(tool,params):
    chain,rpc=tool.rpc(params);target=address(params['contract']);inspector=Inspector(tool.config,chain,rpc,params)
    abi=inspector.abi(target);inspector.canonical_check()
    require(abi is not None,'No verified effective ABI; configure a verified source or provide explicit ABI','ABI_REQUIRED')
    require(inspector.canonicality=='verified','Pinned block could not be checked','CANONICALITY_UNAVAILABLE')
    return abi,inspector


def summary_envelope(response):
    full=response.get('data') or {};s=full.get('structure') or {};v=full.get('verdict')
    body={'view':'summary','analysis_status':full.get('analysis_status'),'structure':{k:s.get(k) for k in ('address','kind','is_proxy','proxy_type','effective_address','resolution_status')},
          'block':full.get('block'),'checked_at':full.get('checked_at'),'patterns':[p.get('id') for p in full.get('patterns',[])],
          'probes_count':len(full.get('probes',[])),'issues_count':len(full.get('issues',[])),'enhancements_missing_count':len(full.get('enhancements_missing',[])),
          'probes':[{k:r.get(k) for k in ('function','status','values')} for r in full.get('probes',[])],
          'issues':full.get('issues',[]),'trimmed':{'probes':0,'issues':0,'verdict_checks':0,'blockers':0,'unknowns':0,'verdict_fields':0},'truncated':False}
    if v:body['verdict']=deepcopy(v)
    out={**response,'data':body,'warnings':[]}
    def size():return len((json.dumps(out,ensure_ascii=False,separators=(',',':'))+'\n').encode())
    if v:
        body['verdict']['entry']=None if v.get('entry') is None else {k:v['entry'].get(k) for k in ('contract','function','status')}
        body['verdict']['blocker_count']=len(v.get('blockers',[]));body['verdict']['unknown_count']=len(v.get('unknowns',[]))
    buckets=[(body,'probes','probes'),(body,'issues','issues')]
    if v:buckets.extend((body['verdict'],key,name) for key,name in [('checks','verdict_checks'),('unknowns','unknowns'),('blockers','blockers')])
    for container,key,name in buckets:
        while size()>2048 and container.get(key):container[key].pop();body['trimmed'][name]+=1;body['truncated']=True
    if size()>2048 and out.get('error'):
        out['error']={k:out['error'].get(k) for k in ('code','retryable')};body['truncated']=True
    if size()>2048:
        body.pop('issues',None);body.pop('probes',None)
        if v:
            body['trimmed']['verdict_fields']=sum(k not in ('answer','confidence','blocker_count','unknown_count') for k in body['verdict'])
            body['verdict']={k:body['verdict'].get(k) for k in ('answer','confidence','blocker_count','unknown_count')}
        body['truncated']=True
    return out
