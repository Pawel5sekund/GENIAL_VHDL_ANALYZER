import re

_ARCH_OBJECTS = re.compile(
    "|".join(
        [
            r"^\s*constant\s+(?P<constant>[\w\s,]+)\s*:",
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
        ]
    ),
    flags=re.I,
).finditer

text1 = "PID : process is"

text2 = "ARCHITECTURE behavioral OF FOC_core IS"

text3 = "END PROCESS PID;"

matches = _GET_SCOPE(text1)

for i in matches:
    print(i.groupdict())


