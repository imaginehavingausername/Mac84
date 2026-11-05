-- Inspect the current accessibility tree; click only one uniquely labelled switch.
on normalizedLabel(rawLabel)
    if rawLabel is missing value then return ""
    set labelText to rawLabel as text
    if labelText ends with ":" then set labelText to text 1 thru -2 of labelText
    return labelText
end normalizedLabel

property lastSeen : ""

on findToggle(wantedLabel)
    tell application "System Events"
        if not (exists process "System Settings") then
            set my lastSeen to "System Settings is not running"
            return missing value
        end if
        tell process "System Settings"
            set windowList to windows
            if (count windowList) is 0 then
                set my lastSeen to "System Settings has no window open"
                return missing value
            end if
            set candidates to {}
            set seenParts to {}
            -- Every window, not only window 1 (v0.3.6 searched window 1 only).
            repeat with winItem in windowList
                set win to contents of winItem
                set winName to "?"
                try
                    set winName to (name of win) as text
                end try
                set switchCount to 0
                set nodes to entire contents of win
                repeat with nodeRef in nodes
                    set matched to false
                    try
                        set nodeRole to value of attribute "AXRole" of nodeRef
                        if nodeRole is "AXCheckBox" or nodeRole is "AXSwitch" then
                            set switchCount to switchCount + 1
                            set nodeName to ""
                            set nodeDescription to ""
                            try
                                set nodeName to my normalizedLabel(name of nodeRef)
                            end try
                            try
                                set nodeDescription to my normalizedLabel(description of nodeRef)
                            end try
                            ignoring case
                                set matched to nodeName is wantedLabel or nodeDescription is wantedLabel
                            end ignoring
                        end if
                    end try
                    if matched then set end of candidates to contents of nodeRef
                end repeat
                set end of seenParts to "'" & winName & "' with " & switchCount & " switches"
            end repeat
            set AppleScript's text item delimiters to ", "
            set my lastSeen to "System Settings showed " & (seenParts as text)
            set AppleScript's text item delimiters to ""
            if (count candidates) is greater than 1 then error "Ambiguous accessibility control: " & wantedLabel
            if (count candidates) is 1 then return item 1 of candidates
        end tell
    end tell
    return missing value
end findToggle

on switchValue(theToggle)
    tell application "System Events" to set rawValue to value of attribute "AXValue" of theToggle
    if rawValue is true or rawValue is 1 or rawValue is "1" then return "1"
    if rawValue is false or rawValue is 0 or rawValue is "0" then return "0"
    error "Accessibility switch has no readable on/off value"
end switchValue

on run argv
    if (count argv) < 2 then error "Missing operation/label"
    set operation to item 1 of argv
    set wantedLabel to item 2 of argv
    if wantedLabel is not "Reduce motion" and wantedLabel is not "Prefer non-blinking cursor" then error "Unsupported control"
    with timeout of 20 seconds
        set theToggle to missing value
        set lastProblem to ""
        -- System Settings may still be launching or switching to the Display
        -- pane (common at restore time, after it was closed). Its window can
        -- vanish mid-search (-1728); retry for ~6 s. The Python caller then
        -- re-opens the Display pane and runs this once more.
        set startedAt to current date
        repeat
            try
                set theToggle to my findToggle(wantedLabel)
            on error errText number errNum
                if errText starts with "Ambiguous" then error errText number errNum
                set lastProblem to " Last error: " & errText
                set theToggle to missing value
            end try
            if theToggle is not missing value then exit repeat
            if ((current date) - startedAt) > 6 then exit repeat
            delay 0.25
        end repeat
        if theToggle is missing value then error "Could not find " & wantedLabel & " in Accessibility > Display (" & lastSeen & "). Check Terminal Accessibility and Automation permissions, or open that page manually." & lastProblem
        set currentValue to my switchValue(theToggle)
        if operation is "read" then return currentValue
        if operation is not "set" or (count argv) is not 4 then error "Invalid operation"
        set desiredValue to item 3 of argv
        set expectedValue to item 4 of argv
        if desiredValue is not "0" and desiredValue is not "1" then error "Invalid switch value"
        if currentValue is not expectedValue then return "conflict"
        if currentValue is desiredValue then return currentValue
        tell application "System Events" to click theToggle
        repeat 20 times
            delay 0.1
            set currentValue to my switchValue(theToggle)
            if currentValue is desiredValue then return currentValue
        end repeat
        error "Control did not change: " & wantedLabel
    end timeout
end run
