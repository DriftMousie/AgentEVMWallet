"""Lexical source evidence, not an AST, an interpreter, or a security proof."""
import hashlib
import re
from .abi import canonical

VERSION='lexical-v1'


def mask(text):
    # Preserve offsets/newlines while excluding comments and literal text from searches.
    token=re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
    return token.sub(lambda m:''.join('\n' if c=='\n' else ' ' for c in m.group()),text)


def closing(text,start,left='(',right=')'):
    depth=0
    for i in range(start,len(text)):
        if text[i]==left:depth+=1
        elif text[i]==right:
            depth-=1
            if depth==0:return i
    return None


def evidence(text,path,start,end):
    return {'file':path,'line':text.count('\n',0,start)+1,'end_line':text.count('\n',0,end)+1,'snippet':text[start:min(end,start+600)],'snippet_truncated':end-start>600}


def extract(text,path,abi):
    if not isinstance(text,str) or len(text.encode())>2*1024*1024:return {'status':'partial','functions':[],'modifiers':[],'coverage':'unsupported_size'}
    clean=mask(text);functions=[];modifiers=[];partial=False;scopes=[];findings=0
    for m in re.finditer(r'\b(contract|interface|library)\s+(\w+)[^{;]*\{',clean):
        begin=clean.find('{',m.start(),m.end());end=closing(clean,begin,'{','}')
        if end is not None:scopes.append((begin,end,m[2],m[1]))
        else:partial=True
    modifier_names=set(re.findall(r'\bmodifier\s+(\w+)',clean))
    function_names=set(re.findall(r'\bfunction\s+(\w+)',clean))
    for m in re.finditer(r'\b(function|modifier)\s+(\w+)\s*(?=\(|\{)',clean):
        if len(functions)+len(modifiers)>=500:partial=True;break
        parent=next((s for s in reversed(scopes) if s[0]<m.start()<s[1]),None)
        if not parent or parent[3]=='interface':continue
        pos=m.end();params=''
        if clean[pos:pos+1]=='(':
            endparams=closing(clean,pos)
            if endparams is None:partial=True;continue
            params=clean[pos+1:endparams];pos=endparams+1
        brace=clean.find('{',pos);semi=clean.find(';',pos)
        if brace<0 or semi>=0 and semi<brace:continue
        end=closing(clean,brace,'{','}')
        if end is None:partial=True;continue
        header=clean[pos:brace];body=clean[brace+1:end]
        # Reject a fake header spanning into another declaration.
        if re.search(r'\b(function|modifier|contract)\b',header):continue
        defs=[i for i in abi or [] if i.get('type')=='function' and i.get('name')==m[2]]
        count=0 if not params.strip() else len(params.split(','))
        defs=[i for i in defs if len(i.get('inputs',[]))==count]
        signature=canonical(defs[0]) if len(defs)==1 else None
        param_names=set(re.findall(r'\b(\w+)\s*(?:,|$)',params))
        row={'name':m[2],'contract':parent[2],'signature':signature,'scope':'source_definition',
             'modifiers':[name for name in re.findall(r'\b\w+\b',header) if name in modifier_names],
             'conditions':[],'calls':[],'evidence':evidence(text,path,m.start(),brace+1)}
        if re.search(r'\bassembly\s*(?:\([^)]*\))?\s*\{',body):
            row['unsupported']='inline_assembly';partial=True
            (modifiers if m[1]=='modifier' else functions).append(row)
            continue
        for req in re.finditer(r'\brequire\s*\(',body):
            if findings>=1000:partial=True;break
            findings+=1
            begin=brace+1+req.start();op=clean.find('(',begin);endreq=closing(clean,op)
            if endreq is None:partial=True;continue
            expression=clean[op+1:endreq];depth=0;split=len(expression)
            for n,ch in enumerate(expression):
                if ch=='(':depth+=1
                elif ch==')':depth-=1
                elif ch==',' and depth==0:split=n;break
            expr=text[op+1:op+1+split].strip();active=expression[:split]
            ids=set(re.findall(r'\b[A-Za-z_]\w*\b',active))
            kind='external' if re.search(r'\b\w+\.(?!mul\b|div\b|add\b|sub\b)\w+\s*\(',active) else 'state' if '(' in active else 'parameter' if ids<=param_names|{'true','false'} else 'unknown'
            if len(expr)>1200:partial=True
            row['conditions'].append({'expr':expr[:1200],'expr_truncated':len(expr)>1200,'kind':kind,'confidence':'syntactic','evidence':evidence(text,path,begin,endreq+1)})
        for call in re.finditer(r'\b(\w+)\.(\w+)\s*\(',body):
            if call[2] in ('mul','div','add','sub'):continue
            if findings>=1000:partial=True;break
            findings+=1
            at=brace+1+call.start()
            row['calls'].append({'target':call[1],'method':call[2],'scope':'syntactic_candidate','evidence':evidence(text,path,at,brace+1+call.end())})
        for call in re.finditer(r'(?<![\w.])([A-Za-z_]\w*)\s*\(',body):
            if call[1] not in function_names and not call[1].startswith('_'):continue
            if findings>=1000:partial=True;break
            findings+=1
            at=brace+1+call.start()
            row['calls'].append({'target':'self','method':call[1],'internal':True,'scope':'internal_or_unresolved_candidate','evidence':evidence(text,path,at,brace+1+call.end())})
        (modifiers if m[1]=='modifier' else functions).append(row)
    return {'status':'partial' if partial else 'extracted','method':'lexical','coverage':'single_file_definitions_not_merged_inheritance',
            'source_sha256':hashlib.sha256(text.encode()).hexdigest(),'file':path,'functions':functions,'modifiers':modifiers,'semantics_partial':partial}
