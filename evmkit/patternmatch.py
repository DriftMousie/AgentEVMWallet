"""Closed, non-executable YAML pattern definitions and bounded integer expressions."""
from pathlib import Path
import re
import yaml
from .abi import canonical,signature_parts
from .common import ToolError,require

ROOT=Path(__file__).with_name('patterns')


class UniqueLoader(yaml.SafeLoader):pass


def mapping(loader,node,deep=False):
    result={}
    for key,value in node.value:
        name=loader.construct_object(key,deep=deep)
        require(isinstance(name,str) and name not in result,'Duplicate/non-string YAML key','PATTERN_INVALID')
        result[name]=loader.construct_object(value,deep=deep)
    return result

UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,mapping)


def parse(text):
    require(isinstance(text,str) and len(text.encode())<=65536,'Pattern exceeds size limit','PATTERN_INVALID')
    try:
        depth=0
        for token in yaml.scan(text):
            require(not isinstance(token,(yaml.tokens.AnchorToken,yaml.tokens.AliasToken)),'YAML aliases/anchors are not supported','PATTERN_INVALID')
            if isinstance(token,(yaml.tokens.BlockMappingStartToken,yaml.tokens.BlockSequenceStartToken,yaml.tokens.FlowMappingStartToken,yaml.tokens.FlowSequenceStartToken)):depth+=1
            elif isinstance(token,(yaml.tokens.BlockEndToken,yaml.tokens.FlowMappingEndToken,yaml.tokens.FlowSequenceEndToken)):depth-=1
            require(depth<=32,'Pattern nesting limit','PATTERN_INVALID')
        data=yaml.load(text,Loader=UniqueLoader)
    except yaml.YAMLError:raise ToolError('PATTERN_INVALID','Invalid or unsafe pattern YAML') from None
    def scalar_types(value):
        if isinstance(value,dict):
            for part in value.values():scalar_types(part)
        elif isinstance(value,list):
            for part in value:scalar_types(part)
        else:require(isinstance(value,(str,int,bool)) or value is None,'Only integer/string/boolean pattern scalars are allowed','PATTERN_INVALID')
    scalar_types(data)
    require(isinstance(data,dict) and set(data)<= {'id','label','match','entries','checks','asset_binding','reads','reads_by_unit','source_guards','route_guard','equations','unknowns','estimate','entry_source_required'} and {'id','match','entries','checks'}<=set(data),'Invalid pattern fields','PATTERN_INVALID')
    require(isinstance(data['id'],str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}',data['id']) is not None and isinstance(data['checks'],list) and len(data['checks'])<=50,'Invalid pattern identity/checks','PATTERN_INVALID')
    match=data['match'];require(isinstance(match,dict) and set(match)<= {'functions_all','functions_any','relations_all'},'Invalid pattern match','PATTERN_INVALID')
    for key,values in match.items():
        require(isinstance(values,list) and all(isinstance(v,str) for v in values),'Invalid pattern requirements','PATTERN_INVALID')
        if key.startswith('functions'):
            for sig in values:signature_parts(sig)
    require(isinstance(data['entries'],dict) and set(data['entries'])=={'shares','assets'},'Pattern needs shares/assets entries','PATTERN_INVALID')
    for row in data['entries'].values():
        require(isinstance(row,dict) and set(row)=={'target','function','args'} and isinstance(row['args'],list),'Invalid entry descriptor','PATTERN_INVALID');signature_parts(row['function'])
    seen=set()
    for row in data['checks']:
        require(isinstance(row,dict) and not set(row)-{'id','target','function','args','field','expect','gte','severity','needs','unit','output_type'} and {'id','target','function','args','severity'}<=set(row),'Invalid check descriptor','PATTERN_INVALID')
        require(row.get('output_type','uint') in ('uint','bool','address','string'),'Unsupported check output type','PATTERN_INVALID')
        require(isinstance(row['id'],str) and row['id'] not in seen and isinstance(row['target'],str) and row['severity'] in ('blocker','info') and isinstance(row['args'],list),'Invalid/duplicate check','PATTERN_INVALID');seen.add(row['id']);signature_parts(row['function'])
        require(row.get('unit','shares') in ('shares','assets') and isinstance(row.get('needs',[]),list),'Invalid check units/needs','PATTERN_INVALID')
    require(data.get('asset_binding') in ('asset','underlying'),'Pattern asset binding must be explicit','PATTERN_INVALID')
    require(isinstance(data.get('equations'),dict) and set(data['equations'])=={'shares','assets'},'Invalid equations','PATTERN_INVALID')
    require(isinstance(data.get('estimate'),dict) and set(data['estimate'])=={'binding','scope','net_known'} and isinstance(data['estimate']['net_known'],bool),'Invalid estimate descriptor','PATTERN_INVALID')
    require(isinstance(data.get('entry_source_required',True),bool),'Invalid source requirement','PATTERN_INVALID')
    require(isinstance(data.get('reads',[]),list) and isinstance(data.get('reads_by_unit',{}),dict),'Invalid read lists','PATTERN_INVALID')
    require(set(data.get('reads_by_unit',{}))<= {'shares','assets'} and all(isinstance(v,list) for v in data.get('reads_by_unit',{}).values()),'Invalid unit read lists','PATTERN_INVALID')
    require(isinstance(data.get('source_guards',[]),list) and isinstance(data.get('unknowns',[]),list),'Invalid source/unknown lists','PATTERN_INVALID')
    route=data.get('route_guard')
    if route is not None:require(isinstance(route,dict) and set(route)=={'modifier','predicate','functions'} and isinstance(route['modifier'],str) and isinstance(route['predicate'],str) and isinstance(route['functions'],dict) and set(route['functions'])=={'shares','assets'},'Invalid route guard','PATTERN_INVALID')
    reads=data.get('reads',[])+sum([data.get('reads_by_unit',{}).get(u,[]) for u in ('shares','assets')],[])
    for row in reads:
        require(isinstance(row,dict) and not set(row)-{'bind','target','relation','function','args'} and isinstance(row.get('bind'),str) and isinstance(row.get('target'),str),'Invalid binding read','PATTERN_INVALID')
        require(('relation' in row)!=('function' in row),'A binding has one read mechanism','PATTERN_INVALID')
        if 'function' in row:signature_parts(row['function']);require(isinstance(row.get('args'),list),'Read needs args','PATTERN_INVALID')
    for row in data.get('source_guards',[]):
        require(isinstance(row,dict) and set(row)=={'id','function','condition_contains','gates'} and all(isinstance(row[k],str) for k in ('id','function','condition_contains')) and isinstance(row['gates'],list),'Invalid source guard','PATTERN_INVALID')
    for row in data.get('unknowns',[]):require(isinstance(row,dict) and set(row)=={'id','reason','target','severity'} and row['severity'] in ('blocker','info'),'Invalid declared unknown','PATTERN_INVALID')
    return data


def load_patterns():
    definitions=[];seen=set()
    for path in sorted(ROOT.glob('*.yaml')):
        row=parse(path.read_text())
        require(row['id']==path.stem and row['id'] not in seen,'Pattern id must match its unique filename','PATTERN_INVALID')
        seen.add(row['id']);definitions.append(row)
    return definitions


def matching_interfaces(abi, requested):
    definitions = load_patterns()
    require(requested == 'auto' or requested in [d['id'] for d in definitions], 'Unknown protocol pattern')
    signatures = {canonical(i) for i in abi or [] if i.get('type') == 'function'}
    for definition in definitions:
        if requested != 'auto' and definition['id'] != requested:
            continue
        rules = definition['match']
        required = set(rules.get('functions_all', []))
        alternatives = set(rules.get('functions_any', []))
        if required <= signatures and (not alternatives or alternatives & signatures):
            yield definition, sorted(required | (alternatives & signatures))


def pattern_hit(definition, functions, relations):
    return {'id':definition['id'], 'label':definition.get('label'),
            'evidence':{'functions':functions, 'relations':relations}, 'definition':definition}


def match_patterns(abi,relations,requested='auto'):
    found = []
    for definition, functions in matching_interfaces(abi, requested):
        names = definition['match'].get('relations_all', [])
        if all(relations.get(name) for name in names):
            found.append(pattern_hit(definition, functions, {name:relations[name] for name in names}))
    return found


def inspect_patterns(inspector, root, requested='auto'):
    """Match verified interfaces using the inspector's pinned-block relation probes."""
    abi = inspector.abi(root)
    found = []
    status = 'complete' if abi is not None else 'unavailable'
    relations = {}
    from .introspect import ZERO
    for definition, functions in matching_interfaces(abi, requested):
        names = definition['match'].get('relations_all', [])
        if abi is None:
            continue
        for name in names:
            if name not in relations:
                relations[name] = inspector.relation(root, name)
            if relations[name] is None:
                probe = inspector.probes.get(inspector.probe_key(root, f'{name}()', []), {})
                if probe.get('status') != 'success' or probe.get('values') != [ZERO]:
                    status = 'partial'
        if all(relations[name] for name in names):
            found.append(pattern_hit(definition, functions, {name:relations[name] for name in names}))
    return found, status, relations


def expression(value,bindings,depth=0,counter=None):
    counter=[0] if counter is None else counter;counter[0]+=1
    require(depth<=16 and counter[0]<=64,'Expression complexity limit','PATTERN_INVALID')
    if isinstance(value,dict):
        if set(value)=={'ref'}:
            require(isinstance(value['ref'],str),'Invalid binding','PATTERN_INVALID')
            resolved=bindings.get(value['ref']);require(resolved is not None,'Required diagnostic value is unknown','VALUE_UNKNOWN');return resolved
        require(set(value)=={'op','args'} and value['op'] in ('add','sub','mul','div_floor','div_ceil') and isinstance(value['args'],list) and len(value['args'])==2,'Unsupported expression','PATTERN_INVALID')
        a,b=[expression(v,bindings,depth+1,counter) for v in value['args']]
        require(not isinstance(a,bool) and not isinstance(b,bool) and str(a).isdigit() and str(b).isdigit(),'Integer operands required','VALUE_UNKNOWN');a,b=int(a),int(b)
        require(value['op'] not in ('div_floor','div_ceil') or b>0,'Division by zero','VALUE_UNKNOWN')
        result={'add':lambda:a+b,'sub':lambda:a-b,'mul':lambda:a*b,'div_floor':lambda:a//b,'div_ceil':lambda:(a+b-1)//b}[value['op']]()
        require(0<=result<2**512,'Arithmetic result outside bounds','VALUE_UNKNOWN');return str(result)
    require(isinstance(value,(str,int,bool)) or value is None,'Unsupported value','PATTERN_INVALID');return value
