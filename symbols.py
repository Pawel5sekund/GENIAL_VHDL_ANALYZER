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
            r"^\s*architecture\s+(?P<architecture_start>\w+)\s+of\s+(?P<arch_entity>\w+)",
            r"^\s*constant\s+(?P<constant>[\w\s,]+)\s*:",
            r"^\s*signal\s+(?P<signal>[\w,\s]+)\s*:",
            r"^\s*type\s+(?P<type>\w+)\s*:",
            r"^\s*shared\s+variable\s+(?P<shared_variable>[\w\s,]+)\s*:",
            r"^\s*end architecture\s+(?P<architecture_end>[\w\s,]+)\s*;",
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
            r"\bprocess\b",
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

    assert False, "Can't determine area from {}".format(dict_)  # pragma: no cover


def _getSymbolsFromText(lines):
    """
    Converts the iterator from _findObjects into a dict, whose key is the
    object's name and the value if the object's info
    """
    objects = {}
    for name, info in _findSymbols(lines):
        if name not in objects:
            objects[name] = info

    return objects


def _findSymbols(lines):
    """
    Returns an iterator with the object name and a dict with info about its
    location
    """
    lnum = 0
    area = None
    for _line in lines:
        line = re.sub(r"\s*--.*", "", _line)
        for match in _GET_SCOPE(line):
            area = _getAreaFromMatch(match.groupdict())

        matches = []
        if area is None:
            matches += _NO_SCOPE_OBJECTS.finditer(line)
        elif area == "entity":
            matches += _ENTITY_OBJECTS(line)
        elif area == "architecture":
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

                    name = submatch.group(submatch.lastindex)

                    #if (key == "architecture_start"):
                    #    name = name+"_start"; 

                    yield name, {
                        "lnum": lnum,
                        "start": start + submatch.start(submatch.lastindex),
                        "end": end + submatch.start(submatch.lastindex),
                        "type": key,
                    }
        lnum += 1
        if _SHOULD_END_SCAN(line):
            break


def _getFirstLocation(lines, objects):
    """Generator that yields objects that are only found once at the
    given buffer and thus are considered unused (i.e., we only found
    its declaration"""

    text = ""
    for line in lines:
        text += re.sub(r"\s*--.*", "", line) + " "

    for _object in objects:
        r_len = 0
        for _ in re.finditer(r"\b%s\b" % _object, text, flags=re.I):
            r_len += 1
            if r_len == 1:
                yield _object


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
                    name = submatch.group(submatch.lastindex)
                    yield {
                        "name": name,
                        "lnum": lnum,
                        "start": start + submatch.start(submatch.lastindex),
                        "end": end + submatch.start(submatch.lastindex),
                        "type": key,
                        "area_name": area_name,
                        "area_type": area,
                        "over_area_name": over_area_name,
                        "architecture": arch_name,
                        "entity": entity_name,
                        "library": "",
                    }
        lnum += 1
        if _SHOULD_END_SCAN(line):
            break

def _getGroupedSymbolsFromText(lines):
    objects = []
    for info in _findGroupedSymbols(lines):
        #if name not in objects:
            objects.append(info)

    return objects


def getSymbols(lines) -> List[DocumentSymbol]:

    "VHDL symbols generation"
    objects = _getGroupedSymbolsFromText(lines) #generating objects from every line of text

    objects_nested = {}

    for _object in objects: #going through every object
        obj_dict = _object #getting dictionary by name/key value
        if not (obj_dict["entity"] in objects_nested.keys()): #looking for the name of entity
            objects_nested[obj_dict["entity"]] = {"values": {},"elements":{}} #when the entity is not on the list - create dictionary key with entity name
            objects_nested[obj_dict["entity"]]["elements"]["ports"] = {} #special subsectors for ports and generics
            objects_nested[obj_dict["entity"]]["elements"]["generics"] = {}
        if not (obj_dict["architecture"] in objects_nested[obj_dict["entity"]]["elements"].keys()): #the same as above
            objects_nested[obj_dict["entity"]]["elements"][obj_dict["architecture"]] = {"values":{},"elements":{}}
        
        if obj_dict["type"] == "signal": #put the specified data types inside specified nested locations 
            objects_nested[obj_dict["entity"]]["elements"][obj_dict["architecture"]]["elements"][_object["name"]] = obj_dict 
        if obj_dict["type"] == "port":
            objects_nested[obj_dict["entity"]]["elements"]["ports"][_object["name"]] = obj_dict
        if obj_dict["type"] == "generic":
            objects_nested[obj_dict["entity"]]["elements"]["generics"][_object["name"]] = obj_dict

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
                    _port = _arch[_ports]
                    symbol_port = DocumentSymbol( #create symbol, based on the data from dictionary
                    name=_ports,
                    kind=SymbolKind.Interface,
                    range=Range(Position(_port["lnum"], _port["start"]),
                        Position(_port["lnum"], _port["end"]),),
                    selection_range=Range(Position(_port["lnum"], _port["start"]),
                                  Position(_port["lnum"], _port["end"]),),
                    detail="PORT",
                    children=[], #no children, because ports don't have inner elements
                    )
                    port_symbols.append(symbol_port) #add port to list, which will be used as children for "PORT" section
                symbol_port = DocumentSymbol(
                name="PORT",
                kind=SymbolKind.Struct,
                range=Range(Position(0, 0), #TODO
                    Position(0, 0),),
                selection_range=Range(Position(0, 0),
                    Position(0, 0),),
                detail="",
                children=port_symbols,
                )
                #entity_symbols.append(symbol_port)
                entity_symbols.extend(port_symbols)
            elif _arches == "generics":
                for _generics in _arch:
                    _generic = _arch[_generics]
                    symbol_generic = DocumentSymbol(
                    name=_generics,
                    kind=SymbolKind.Constant,
                    range=Range(Position(_generic["lnum"], _generic["start"]),
                        Position(_generic["lnum"], _generic["end"]),),
                    selection_range=Range(Position(_generic["lnum"], _generic["start"]),
                                  Position(_generic["lnum"], _generic["end"]),),
                    detail="GENERIC",
                    children=[],
                    )
                    generic_symbols.append(symbol_generic)
                symbol_generic = DocumentSymbol(
                name="GENERIC",
                kind=SymbolKind.Struct,
                range=Range(Position(0, 0),
                    Position(0, 0),),
                selection_range=Range(Position(0, 0),
                    Position(0, 0),),
                detail="",
                children=generic_symbols,
                )
                #entity_symbols.append(symbol_generic)
                entity_symbols.extend(generic_symbols)
            elif _arches == "":
                empty = 1
            else:
                for _signals in _arch["elements"]:
                    _signal = _arch["elements"][_signals]
                    symbol_signal = DocumentSymbol(
                    name=_signals,
                    kind=SymbolKind.Field,
                    range=Range(Position(_signal["lnum"], _signal["start"]),
                        Position(_signal["lnum"], _signal["end"]),),
                    selection_range=Range(Position(_signal["lnum"], _signal["start"]),
                                  Position(_signal["lnum"], _signal["end"]),),
                    detail="SIGNAL",
                    children=[],
                    )
                    arch_symbols.append(symbol_signal)
                
                symbol_arch = DocumentSymbol(
                name=_arches,
                kind=SymbolKind.Method,
                range=Range(Position(0, 0),
                    Position(0, 0),),
                selection_range=Range(Position(0, 0),
                    Position(0, 0),),
                detail="ARCHITECTURE",
                children=arch_symbols,
                )
                entity_symbols.append(symbol_arch)
        if not (_entities == ""):
            symbol_entity = DocumentSymbol(
            name=_entities,
            kind=SymbolKind.Class,
            range=Range(Position(0, 0),
                    Position(0, 0),),
            selection_range=Range(Position(0, 0),
                    Position(0, 0),),
            detail="ENTITY",
            children=entity_symbols,
            )
            grouped_symbols.append(symbol_entity)

    f = open("/home/pawel5sekund/objects.txt", "w")
    for _object in objects:
        f.write(json.dumps(_object))
        f.write("\r\n")
    f.close()

    return grouped_symbols
