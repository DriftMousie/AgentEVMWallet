"""Exact whole-word edits and time annotations; never guess unknown ABI offsets."""
import re
import time
from .abi import canonical
from .common import address,hexdata,require,uint
from .standards import entries,V2_ROUTER

TIME_NAMES={"deadline","expiry","validuntil"}
RECIPIENT_NAMES={"recipient","to","receiver"}


def input_fields(row):
    raw=row.get("input")
    if raw is None: return {"selector":None,"input_bytes":None,"input_available":False}
    raw=hexdata(raw)
    return {"selector":raw[:10] if len(raw)>=10 else None,"input_bytes":(len(raw)-2)//2,"input_available":True}


def patch_word(data,word,value,type_="bytes32"):
    data=hexdata(data);word=uint(word,"zero-based word index")
    require(type_ in ("address","uint256","bytes32"),"type must be address, uint256 or bytes32")
    start=4+32*word;raw=bytes.fromhex(data[2:])
    require(len(raw)>=4 and start+32<=len(raw),"Word must identify an existing complete 32-byte word after the selector")
    if type_=="address": replacement=bytes.fromhex(address(value)[2:]).rjust(32,b"\0")
    elif type_=="uint256":
        if isinstance(value,str) and re.fullmatch(r"0x[0-9a-fA-F]+",value): value=int(value,16)
        replacement=uint(value,"uint256 word value").to_bytes(32,"big")
    else:
        value=hexdata(value);require(len(value)==66,"bytes32 value must contain exactly 32 bytes; use --type address for an address")
        replacement=bytes.fromhex(value[2:])
    result=raw[:start]+replacement+raw[start+32:]
    return "0x"+result.hex(),{"word_index":word,"byte_offset":start,"type":type_,"before":"0x"+raw[start:start+32].hex(),"after":"0x"+replacement.hex()}


def validate_field_map(value,data):
    require(isinstance(value,dict) and not set(value)-{"recipient","deadline"},"field_map supports recipient and deadline only")
    used=set()
    for name,spec in value.items():
        require(isinstance(spec,dict) and not set(spec)-{"word","type","unit"} and "word" in spec,"Field mapping needs a word index and explicit type")
        index=uint(spec["word"],"mapped word")
        require(index not in used,"Mapped fields cannot share a word");used.add(index)
        expected="address" if name=="recipient" else "uint256"
        require(spec.get("type")==expected,f"{name} mapping requires type {expected}")
        if name=="deadline": require(spec.get("unit")=="seconds","deadline mapping requires unit=seconds")
        patch_word(data,index,"0x"+"00"*20 if name=="recipient" else "0",expected)
    return value


def parameter_schema(decoded):
    if decoded.get("parameter_schema") is not None: return decoded["parameter_schema"]
    signature=decoded.get("signature")
    matches=[r["abi"]["inputs"] for r in entries("function") if canonical(r["abi"])==signature]
    if matches and all(m==matches[0] for m in matches): return matches[0]
    return [{"name":name,"type":typ} for name,typ in zip(decoded.get("named_args",{}),decoded.get("types",[]))]


def named_fields(decoded,limit=1000):
    if decoded.get("status")!="decoded": return []
    result=[];visited=0
    def walk(spec,value,path,depth):
        nonlocal visited
        visited+=1
        if visited>limit or depth>64:return
        typ=spec["type"];name=spec.get("name","").lstrip("_").lower()
        if typ.endswith("]"):
            if not isinstance(value,(list,tuple)):return
            base=typ[:typ.rfind("[")]
            for i,element in enumerate(value[:limit]):walk({**spec,"type":base},element,path+[i],depth+1)
        elif typ.startswith("tuple") and isinstance(value,(list,tuple)):
            for i,(part,element) in enumerate(zip(spec.get("components",[]),value)):walk(part,element,path+[i],depth+1)
        elif name in TIME_NAMES and re.fullmatch(r"uint(?:[0-9]+)?",typ):result.append({"kind":"deadline","path":path,"name":spec.get("name"),"type":typ,"value":value})
        elif name in RECIPIENT_NAMES and typ=="address":result.append({"kind":"recipient","path":path,"name":spec.get("name"),"type":typ,"value":value})
    for i,(spec,value) in enumerate(zip(parameter_schema(decoded),decoded.get("args",[]))):walk(spec,value,[i],0)
    return result


def resolve_time(value,now):
    if isinstance(value,str) and re.fullmatch(r"now\+[0-9]+",value):return str(uint(now+uint(value[4:],"relative seconds"),"deadline"))
    return str(uint(value,"deadline in seconds"))


def timing(decoded,now=None,source="local_utc_clock",field_map=None,data=None):
    now=int(time.time()) if now is None else int(now)
    found=named_fields(decoded)
    fields=[]
    for f in found:
        if f["kind"]!="deadline":continue
        fields.append({"field_path":f["path"],"name":f["name"],"raw":str(f["value"]),"semantic_source":"abi_parameter_name","type":f["type"]})
    if field_map and "deadline" in field_map:
        spec=field_map["deadline"];raw=bytes.fromhex(hexdata(data)[2:]);start=4+32*int(spec["word"])
        require(start+32<=len(raw),"Mapped deadline word is incomplete")
        fields.append({"field_path":None,"word_index":int(spec["word"]),"name":"deadline","raw":str(int.from_bytes(raw[start:start+32],"big")),"semantic_source":"explicit_word_mapping","type":"uint256"})
    known=decoded.get("signature") in {canonical(item) for item in V2_ROUTER}
    for f in fields:
        value=uint(f["raw"]);bits=int(f["type"][4:] or 256)
        special=value in (0,2**bits-1)
        plausible=value<=253402300799
        understood=known or plausible and not special
        f.update(unit="seconds" if understood else "unknown_or_special",expired=now>value if understood else None,
                 expires_in_s=str(value-now) if understood else None,deadline=str(value),
                 semantic_confidence="standard_pattern" if known else "user_mapping" if f["semantic_source"]=="explicit_word_mapping" else "name_based_assumption")
    return {"reference_timestamp":now,"clock_source":source,"fields":fields}


def refresh_timing(result):
    now=int(time.time());risks=[r for r in result.get("risks",[]) if r.get("generated_by")!="timing"]
    groups=[(result,result.get("input_decode",{}),{})]
    groups.extend((row,row.get("decoded") or {},{"call_path":row.get("path")}) for row in result.get("internal_calls",{}).get("calls",[]))
    groups.extend((row,row.get("decoded") or {},{"execution_index":row.get("execution_index")}) for row in result.get("declared_calls",{}).get("calls",[]))
    for owner,decoded,context in groups:
        owner["timing"]=timing(decoded,now)
        for field in owner["timing"]["fields"]:
            if field["expired"] is True:
                risks.append({"code":"DEADLINE_EXPIRED","severity":"warning","generated_by":"timing","field_path":field["field_path"],
                              "deadline":field["deadline"],"clock_source":"local_utc_clock","message":"Recognized time parameter is in the past; replay must be simulated",**context})
    result["risks"]=risks
    return result
