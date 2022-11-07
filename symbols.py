# This file is part of HDL Checker.
#
# Copyright (c) 2015 - 2019 suoto (Andre Souto)
#
# HDL Checker is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# HDL Checker is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with HDL Checker.  If not, see <http://www.gnu.org/licenses/>.
"VHDL static checking to find unused signals, ports and constants."

import logging
import re
from typing import Dict, List, Tuple
import json

#  from hdl_checker.path import Path
from hdl_checker.diagnostics import (
    DiagType,
    LibraryShouldBeOmited,
    ObjectIsNeverUsed,
    StaticCheckerDiag,
)

from pygls.types import (
    CompletionItem,
    CompletionItemKind,
    Diagnostic,
    DiagnosticSeverity,
    DocumentSymbol,
    InsertTextFormat,
    Location,
    MarkupContent,
    MarkupKind,
    Position,
    Range,
    SymbolInformation,
    SymbolKind,
)

_logger = logging.getLogger(__name__)

_GET_SCOPE = re.compile(
    "|".join(
        [
            r"^\s*entity\s+(?P<entity_name>\w+)\s+is\b",
            r"^\s*architecture\s+(?P<architecture_name>\w+)\s+of\s+(?P<arch_entity>\w+)",
            r"^\s*package\s+(?P<package_name>\w+)\s+is\b",
            r"^\s*package\s+body\s+(?P<package_body_name>\w+)\s+is\b",
            r"^\s*(?P<process_name>[\w]+)\s*:\s*process\s*is\s*",
            r"^\s*end\s*process\s+(?P<process_end>[\w\s,]+)\s*;",  
            r"^\s*end\s*architecture\s+(?P<architecture_end>[\w\s,]+)\s*;",
            r"^\s*end entity\s+(?P<entity_end>[\w,\s]+)\s*;",
            r"^\s*end\s+(?P<end_of>[\w\s,]+)\s*",
        ]
    ),
    flags=re.I,
).finditer

_NO_SCOPE_OBJECTS = re.compile(
    "|".join(
        [
            r"^\s*library\s+(?P<library>[\w\s,]+)",
            r"^\s*attribute\s+(?P<attribute>[\w\s,]+)\s*:",
        ]
    ),
    flags=re.I,
)

_ENTITY_OBJECTS = re.compile(
    "|".join(
        [
            r"^\s*(?P<port>[\w\s,]+)\s*:\s*(in|out|inout|buffer|linkage)\s+\w+",
            r"^\s*(?P<generic>[\w\s,]+)\s*:\s*\w+",
            r"^\s*entity\s+(?P<entity_start>\w+)\s+is\b",
            r"^\s*end entity\s+(?P<entity_end>[\w,\s]+)\s*;",
        ]
    ),
    flags=re.I,
).finditer

_ARCH_OBJECTS = re.compile(
    "|".join(
        [
            r"^\s*constant\s+(?P<constant>[\w\s,]+)\s*:",
            r"^\s*end\s+(?P<end_of>[\w\s,]+)\s*",
            r"^\s*signal\s+(?P<signal>[\w,\s]+)\s*:",
            r"^\s*type\s+(?P<type>\w+)\s*:",
            r"^\s*shared\s+variable\s+(?P<shared_variable>[\w\s,]+)\s*:",
            r"^\s*architecture\s+(?P<architecture_start>\w+)\s+of\b",
            r"^\s*end\s*architecture\s+(?P<architecture_end>[\w\s,]+)\s*;",
            r"^\s*(?P<process_start>[\w]+)\s*:\s*process\s*is\s*",
            r"^\s*end\s*process\s+(?P<process_end>[\w\s,]+)\s*;",         
        ]
    ),
    flags=re.I,
).finditer

_SHOULD_END_SCAN = re.compile(
    "|".join(
        [
            r"\bgeneric\s+map",
            r"\bport\s+map",
            r"\bgenerate\b",
            r"\w+\s*:\s*entity",
            #r"\bprocess\b",
        ]
    )
).search


def _getAreaFromMatch(dict_):  # pylint: disable=inconsistent-return-statements
    """
    Returns code area based on the match dict
    """
    if dict_["entity_name"] is not None:
        return "entity"
    if dict_["architecture_name"] is not None:
        return "architecture"
    if dict_["package_name"] is not None:
        return "package"
    if dict_["package_body_name"] is not None:
        return "package_body"
    if dict_["process_name"] is not None:
        return "process"
    if dict_["process_end"] is not None:
        return "process_end"
    if dict_["architecture_end"] is not None:
        return "architecture_end"
    if dict_["entity_end"] is not None:
        return "entity_end"
    if dict_["end_of"] is not None:
        return "end_of"

    assert False, "Can't determine area from {}".format(dict_)  # pragma: no cover


__COMMENT_TAG_SCANNER__ = re.compile(
    "|".join([r"\s*--\s*(?P<tag>TODO|FIXME|XXX)\s*:\s*(?P<text>.*)"])
)


def _getCommentTags(lines):
    """
    Generates diags from 'TODO', 'FIXME' and 'XXX' tags
    """
    result = []
    lnum = 0
    for line in lines:
        lnum += 1
        line_lc = line.lower()
        skip_line = True
        for tag in ("todo", "fixme", "xxx"):
            if tag in line_lc:
                skip_line = False
                break
        if skip_line:
            continue

        for match in __COMMENT_TAG_SCANNER__.finditer(line):
            _dict = match.groupdict()
            result += [
                StaticCheckerDiag(
                    line_number=lnum - 1,
                    column_number=match.start(match.lastindex - 1),
                    severity=DiagType.STYLE_INFO,
                    text="%s: %s" % (_dict["tag"].upper(), _dict["text"]),
                )
            ]
    return result


def _getMiscChecks(objects):
    """
    Get generic code hints (or it should do that sometime in the future...)
    """
    if "library" not in [x["type"] for x in objects.values()]:
        return

    for library, obj in objects.items():
        if obj["type"] != "library":
            continue
        if library == "work":
            yield LibraryShouldBeOmited(
                line_number=obj["lnum"], column_number=obj["start"], library=library
            )

def _findGroupedSymbols(lines):
    """
    Returns an iterator with the object name and a dict with info about its
    location
    """
    lnum = 0
    area = None
    area_name = ""
    over_area_name = ""
    arch_name = ""
    entity_name = ""
    proc_name = ""
    proc_end_bool = False
    entity_end_bool = False
    arch_end_bool = False   
    for _line in lines:
        line = re.sub(r"\s*--.*", "", _line)
        for match in _GET_SCOPE(line):
            iter = 0
            for key, value in match.groupdict().items():
                if value is None:
                    continue
                else:
                    if iter == 0:
                        area_name = value
                    else:
                        over_area_name = value
                    iter+=1
                    
                    
            area = _getAreaFromMatch(match.groupdict())
        

        matches = []
        if area is None:
            matches += _NO_SCOPE_OBJECTS.finditer(line)
        elif area == "entity":
            arch_name=""
            entity_name = area_name
            matches += _ENTITY_OBJECTS(line)
        elif area == "architecture":
            matches += _ARCH_OBJECTS(line)
            arch_name = area_name
            entity_name = over_area_name
        elif area == "process":
            matches += _ARCH_OBJECTS(line)
            proc_name = area_name
        elif area == "process_end":
            matches += _ARCH_OBJECTS(line)
            proc_end_bool = True
        elif area == "architecture_end":
            matches += _ARCH_OBJECTS(line)
            arch_end_bool = True
        elif area == "entity_end":
            matches += _ENTITY_OBJECTS(line)
            entity_end_bool = True
        elif area == "end_of":
            if proc_name != "":
                proc_end_bool = True
                matches += _ARCH_OBJECTS(line)
            elif arch_name != "": 
                arch_end_bool = True
                matches += _ARCH_OBJECTS(line)
            elif entity_name != "":
                entity_end_bool = True
                matches += _ARCH_OBJECTS(line)

        
        for match in matches:
            for key, value in match.groupdict().items():
                if value is None:
                    continue
                _group_d = match.groupdict()
                index = match.lastindex
                if "port" in _group_d.keys() and _group_d["port"] is not None:
                    index -= 1
                start = match.start(index)
                end = match.end(index)

                # More than 1 declaration can be done in a single line.
                # Must strip white spaces and commas properly
                for submatch in re.finditer(r"(\w+)", value):
                    # Need to decrement the last index because we have a group that
                    # catches the port type (in, out, inout, etc)

                    if proc_end_bool and key == "end_of":
                        key = "process_end"
                    elif arch_end_bool and key == "end_of":
                        key = "architecture_end"
                    elif entity_end_bool and key == "end_of":
                        key = "entity_end"
                    
                    name = submatch.group(submatch.lastindex)
                    yield {
                        "name": name,
                        "lnum": lnum,
                        "start": start + submatch.start(submatch.lastindex),
                        "end": end + submatch.start(submatch.lastindex),
                        "type": key,
                        "architecture": arch_name,
                        "entity": entity_name,
                        "process": proc_name,
                        "library": "",
                    }

        if proc_end_bool == True:
            proc_end_bool = False
            proc_name = ""
        if arch_end_bool == True:
            arch_end_bool = False
            arch_name = ""
        if entity_end_bool == True:
            entity_end_bool = False
            entity_name = ""
        lnum += 1
        if _SHOULD_END_SCAN(line):
            break

def _getGroupedSymbolsFromText(lines):
    objects = []
    for info in _findGroupedSymbols(lines):
        #if name not in objects:
            objects.append(info)

    return objects

def _symbolGeneration(name = "", properties = [], detail = "", kind = SymbolKind.Interface, child = ["default"], target = ["default"]):
    
    #check format
    if name == "":
        return
    try:
        isGroup = False

        if(len(child) > 0):
            if isinstance(child[0], str) == False:
                isGroup = True
        elif (len(child) == 0):
            isGroup = True
        
        if (isGroup == False):
            symbol = DocumentSymbol( #create symbol, based on the data from dictionary
            name=name,
            kind=kind,
            range=Range(Position(properties["lnum"], properties["start"]),
                        Position(properties["lnum"], properties["end"]),),
            selection_range=Range(Position(properties["lnum"], properties["start"]),
                                Position(properties["lnum"], properties["end"]),),
            detail=detail,
            children=[], #no children, because ports don't have inner elements
            )
        else:
            if(properties["values"]["start"]["lnum"] == "" or properties["values"]["end"]["lnum"] == "" or properties["values"]["end"]["end"] == ""):
                return
            symbol = DocumentSymbol(
            name=name,
            kind=kind,
            range=Range(Position(properties["values"]["start"]["lnum"], 0),
                        Position(properties["values"]["end"]["lnum"], properties["values"]["end"]["end"]),),
            selection_range=Range(Position(properties["values"]["start"]["lnum"], 0),
                                Position(properties["values"]["end"]["lnum"], properties["values"]["end"]["end"]),),
            detail=detail,
            children=child,
            )
        
        target.append(symbol)
    except:
        print("Adding error")

_empty_symbol = DocumentSymbol(
        name="EMPTY",
        kind=SymbolKind.Interface,
        range=Range(Position(0, 0),
                    Position(0, 0),),
        selection_range=Range(Position(0, 0),
                              Position(0, 0),),
        detail="EMPTY",
        children=[],
        )

def getSymbols(lines) -> List[DocumentSymbol]:

    "VHDL symbols generation"
    objects = _getGroupedSymbolsFromText(lines) #generating objects from every line of text

    objects_nested = {}
    
    #f = open("/home/pawel5sekund/objects.txt", "w")
    #for _object in objects:
    #    f.write(json.dumps(_object))
    #    f.write("\r\n")
    #f.close()

    for _object in objects: #going through every object
        obj_dict = _object #getting dictionary by name/key value
        if not (obj_dict["entity"] in objects_nested.keys()): #looking for the name of entity
            objects_nested[obj_dict["entity"]] = {"values": {},"elements":{}} #when the entity is not on the list - create dictionary key with entity name
            objects_nested[obj_dict["entity"]]["elements"]["ports"] = {} #special subsectors for ports and generics
            objects_nested[obj_dict["entity"]]["elements"]["generics"] = {}
            objects_nested[obj_dict["entity"]]["elements"]["architectures"] = {}
        if not (obj_dict["architecture"] in objects_nested[obj_dict["entity"]]["elements"]["architectures"].keys()): #the same as above
            objects_nested[obj_dict["entity"]]["elements"]["architectures"][obj_dict["architecture"]] = {"values":{},"elements":{}}
            objects_nested[obj_dict["entity"]]["elements"]["architectures"][obj_dict["architecture"]]["elements"]["signals"] = {}
            objects_nested[obj_dict["entity"]]["elements"]["architectures"][obj_dict["architecture"]]["elements"]["processes"] = {}
        if not (obj_dict["process"] in objects_nested[obj_dict["entity"]]["elements"]["architectures"][obj_dict["architecture"]]["elements"]["processes"].keys()): #the same as above
            objects_nested[obj_dict["entity"]]["elements"]["architectures"][obj_dict["architecture"]]["elements"]["processes"][obj_dict["process"]] = {"values":{},"elements":{}}
        
        if obj_dict["type"] == "signal": #put the specified data types inside specified nested locations 
            objects_nested[obj_dict["entity"]]["elements"]["architectures"][obj_dict["architecture"]]["elements"]["signals"][_object["name"]] = obj_dict 
        if obj_dict["type"] == "port":
            objects_nested[obj_dict["entity"]]["elements"]["ports"][_object["name"]] = obj_dict
        if obj_dict["type"] == "generic":
            objects_nested[obj_dict["entity"]]["elements"]["generics"][_object["name"]] = obj_dict
        if obj_dict["type"] == "entity_start":
            objects_nested[obj_dict["entity"]]["values"]["start"] = obj_dict
        if obj_dict["type"] == "entity_end":
            objects_nested[obj_dict["entity"]]["values"]["end"] = obj_dict
        if obj_dict["type"] == "architecture_start":
            objects_nested[obj_dict["entity"]]["elements"]["architectures"][obj_dict["architecture"]]["values"]["start"] = obj_dict
        if obj_dict["type"] == "architecture_end":
            objects_nested[obj_dict["entity"]]["elements"]["architectures"][obj_dict["architecture"]]["values"]["end"] = obj_dict
        if obj_dict["type"] == "process_start":
            objects_nested[obj_dict["entity"]]["elements"]["architectures"][obj_dict["architecture"]]["elements"]["processes"][obj_dict["process"]]["values"]["start"] = obj_dict
        if obj_dict["type"] == "process_end":
            objects_nested[obj_dict["entity"]]["elements"]["architectures"][obj_dict["architecture"]]["elements"]["processes"][obj_dict["process"]]["values"]["end"] = obj_dict

    #f = open("/home/pawel5sekund/objects.txt", "w")
    #f.write(json.dumps(objects_nested))
    #f.close()


    grouped_symbols = []

    entity_symbols = []
    arch_symbols = []
    port_symbols = []
    generic_symbols = []
    for _entities in objects_nested:    
        entity_symbols = [] #cleaning before new entity analyze
        arch_symbols = []
        port_symbols = []
        generic_symbols = []
        if _entities == "":
            continue
        _entity = objects_nested[_entities]
        for _arches in _entity["elements"]: #go through every element/key of dictionary
            _arch = _entity["elements"][_arches] #select the element from dictionary

            if _arches == "ports": #specified group for ports in entity (because eentity can have move than one architecture, but only one generic and port definition)
                for _ports in _arch:
                    _symbolGeneration(name = _ports, properties = _arch[_ports], detail = "PORT", kind = SymbolKind.Interface, target = entity_symbols) #add port to list, which will be used as children for "PORT" section

            elif _arches == "generics":
                for _generics in _arch:
                    #generics generation
                    _symbolGeneration(name = _generics, properties = _arch[_generics], detail = "GENERIC", kind = SymbolKind.Constant, target = entity_symbols)

            elif _arches == "architectures":
                for _inarches in _arch:
                    _inarch = _arch[_inarches]
                    for _elinarches in _inarch["elements"]:
                        _elinarch = _inarch["elements"][_elinarches]
                        if _elinarches == "signals":
                            for _signals in _elinarch:

                                _symbolGeneration(name = _signals, properties = _elinarch[_signals], detail = "SIGNAL", kind = SymbolKind.Field, target = arch_symbols)

                        if _elinarches == "processes":
                            for _processes in _elinarch:

                                _symbolGeneration(name = _processes, properties = _elinarch[_processes], detail = "PROCESS", kind = SymbolKind.Field, child = [_empty_symbol], target = arch_symbols)

                        else:
                            if(_elinarches == ""):
                                continue #looking for only properly formatted

                    #generate architecture with childs in specific entity
                    _symbolGeneration(name = _inarches, properties = _arch[_inarches], detail = "ARCHITECTURE", kind = SymbolKind.Method, child = arch_symbols, target = entity_symbols)
        
        #generate entity with childs in the main group of symbols
        _symbolGeneration(name = _entities, properties = objects_nested[_entities], detail = "ENTITY", kind = SymbolKind.Class, child = entity_symbols, target = grouped_symbols)

    return grouped_symbols, objects_nested
