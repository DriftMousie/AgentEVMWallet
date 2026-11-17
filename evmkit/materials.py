"""Bounded verified source normalization. Remote paths are labels, never filenames."""
import hashlib
import json
from web3 import Web3
from .common import ToolError, address, require

MAX_SOURCE_BYTES=2*1024*1024


def normalize_sources(value,main=None):
    if isinstance(value,str):
        text=value.strip()
        candidate=text[1:-1] if text.startswith('{{') and text.endswith('}}') else text
        try:parsed=json.loads(candidate)
        except (ValueError,TypeError):parsed=None
        if isinstance(parsed,dict):value=parsed.get('sources',parsed)
        else:value={main or 'Contract.sol':{'content':value}}
    if not isinstance(value,dict):return {}
    if 'sources' in value and isinstance(value['sources'],dict):value=value['sources']
    result={};size=0
    for name,record in list(value.items())[:100]:
        content=record.get('content') if isinstance(record,dict) else record if isinstance(record,str) else None
        if not isinstance(name,str) or not isinstance(content,str):continue
        try:encoded=content.encode('utf-8')
        except UnicodeError:raise ToolError('SOURCE_TEXT_INVALID','Source is not valid UTF-8 text') from None
        size+=len(encoded)
        require(size<=MAX_SOURCE_BYTES,'Verified source text exceeds 2 MiB limit','SOURCE_TOO_LARGE')
        result[name[:512]]={'content':content,'sha256':hashlib.sha256(encoded).hexdigest()}
    return result


def material(sources,target,code_hash,include_source=False):
    target=address(target)
    info=sources.contract(target,code_hash=code_hash)
    out={'abi':info.get('abi'),'verified':info.get('is_verified'),'name':info.get('contract_name'),
         'provider':info.get('source'),'metadata':info,'files':{},'main_path':None,'source_status':'not_requested'}
    if not include_source:return out
    out['source_status']='unavailable'
    if not sources.enabled:return out
    candidates=[]
    try:
        row=sources.blockscout('/api/v2/smart-contracts/'+target,context=code_hash)
        if isinstance(row.get('abi'),list) and isinstance(row.get('source_code'),str):
            main=row.get('file_path') or 'Contract.sol';files={main:{'content':row['source_code']}}
            for extra in (row.get('additional_sources') or [])[:99]:
                if isinstance(extra,dict) and isinstance(extra.get('source_code'),str):files[extra.get('file_path') or 'Additional.sol']={'content':extra['source_code']}
            candidates.append((files,main,'blockscout'))
    except (ToolError,TypeError,AttributeError):pass
    if not candidates and sources.provider.get('sourcify',False):
        try:
            base=sources.provider.get('sourcify_url') or sources.raw.get('sourcify',{}).get('base_url','https://sourcify.dev/server')
            row=sources.cached_get(base,f'/v2/contract/{sources.chain["chain_id"]}/{target}',
                {'fields':'abi,sources,compilation,runtimeBytecode.onchainBytecode'},context=code_hash)
            require(str(row.get('chainId'))==str(sources.chain['chain_id']) and address(row.get('address'))==target,'Source identity mismatch','SOURCE_UNAVAILABLE')
            require(row.get('runtimeMatch') in ('match','exact_match'),'Source runtime not verified','SOURCE_UNAVAILABLE')
            code=(row.get('runtimeBytecode') or {}).get('onchainBytecode')
            require(isinstance(code,str) and Web3.to_hex(Web3.keccak(hexstr=code))==code_hash,'Source runtime code mismatch','SOURCE_UNAVAILABLE')
            qualified=(row.get('compilation') or {}).get('fullyQualifiedName','')
            candidates.append((row.get('sources'),qualified.rsplit(':',1)[0] if ':' in qualified else None,'sourcify'))
        except (ToolError,ValueError,TypeError,AttributeError):pass
    if not candidates and sources.provider.get('etherscan',False):
        try:
            row=sources.etherscan(target,context=code_hash)
            require(row.get('ABI')!='Contract source code not verified' and row.get('SourceCode'),'Source unavailable','SOURCE_UNAVAILABLE')
            candidates.append((row['SourceCode'],row.get('ContractFileName') or None,'etherscan'))
        except (ToolError,TypeError,AttributeError):pass
    for value,main,provider in candidates:
        try:
            files=normalize_sources(value,main)
            if not files:continue
            if main not in files:main=next(iter(files)) if len(files)==1 else None
            if main is None:
                sources.warn('SOURCE_MAIN_FILE_UNKNOWN');continue
            out.update(files=files,main_path=main,source_provider=provider,source_status='available')
            return out
        except (ToolError,ValueError,TypeError):sources.warn('SOURCE_TEXT_UNAVAILABLE')
    sources.warn('SOURCE_TEXT_UNAVAILABLE')
    return out
