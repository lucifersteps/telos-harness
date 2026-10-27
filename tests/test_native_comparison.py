"""Exercise native comparison and section protocol without accessing Apple stores."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which('swift') is None, reason='Requires the native Swift runtime')
def test_native_comparison_ignores_only_top_level_section_diagnostics(tmp_path):
    helper=Path(__file__).resolve().parents[1]/'checkout/src/telos/connectors/apple/personal_helper.swift'
    source=helper.read_text()
    start=source.index('func comparableAppleState(')
    end=source.index('\n}\n',start)+3
    check=source[start:end]+r'''
func equal(_ lhs:[String:Any],_ rhs:[String:Any])->Bool {
    NSDictionary(dictionary:comparableAppleState(lhs)).isEqual(to:comparableAppleState(rhs))
}
let original:[String:Any] = ["title":"Prepare talk", "section_id":NSNull(),
    "modified_at":"2026-10-27T08:00:00+08:00", "section_read_error":"NSError 0x1111"]
var refreshed=original
refreshed["section_read_error"]="NSError 0x2222"
precondition(equal(original,refreshed))
refreshed.removeValue(forKey:"section_read_error")
precondition(equal(original,refreshed))
for (key,value) in [("title","Revised talk"),("section_id","SECTION-001"),
                    ("modified_at","2026-10-27T08:01:00+08:00")] {
    var changed=refreshed;changed[key]=value
    precondition(!equal(original,changed), "Must retain conflict for \(key)")
}
var knownSection=refreshed;knownSection["section_id"]=""
precondition(!equal(original,knownSection), "Unknown membership differs from sectionless")
let nestedA:[String:Any] = ["attachment":["section_read_error":"first"]]
let nestedB:[String:Any] = ["attachment":["section_read_error":"second"]]
precondition(!equal(nestedA,nestedB), "Unrelated nested data must not be filtered")
precondition(original["section_read_error"] as? String == "NSError 0x1111")
print("native comparison contract passed")
'''
    result=subprocess.run(['swift','-module-cache-path',str(tmp_path/'swift-cache'),'-e','import Foundation\n'+check],capture_output=True,text=True,timeout=90)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'native comparison contract passed' in result.stdout


@pytest.mark.skipif(shutil.which('swift') is None, reason='Requires the native Swift runtime')
def test_native_section_parameters_decode_required_top_level_grocery_flag(tmp_path):
    helper=Path(__file__).resolve().parents[1]/'checkout/src/telos/connectors/apple/personal_helper.swift'
    source=helper.read_text()
    start=source.index('func sectionInvocationParameters(')
    end=source.index('\n}\n',start)+3
    check=source[start:end]+r'''
struct NativeList:Decodable { let objectIDUUID:String }
struct BaseOption:Decodable { let base:String }
struct NativeConfiguration:Decodable {
    let sortingStyle:BaseOption
    let showCompleted:BaseOption
    let countCompleted:Bool
    let remindersPrefetch:BaseOption
    let fetchSubtasks:String
}
struct NativeParameters:Decodable {
    let list:NativeList
    let fetchPredefinedGroceryListSections:Bool
    let configuration:NativeConfiguration
}
let listID="00000000-0000-0000-0000-000000000001"
let parameters=sectionInvocationParameters(listID)
let data=try PropertyListSerialization.data(fromPropertyList:parameters,format:.binary,options:0)
let native=try PropertyListDecoder().decode(NativeParameters.self,from:data)
precondition(native.list.objectIDUUID==listID)
precondition(native.fetchPredefinedGroceryListSections==false)
precondition(native.configuration.sortingStyle.base=="manual")
precondition(native.configuration.showCompleted.base=="on")
precondition(native.configuration.countCompleted==true)
precondition(native.configuration.remindersPrefetch.base=="none")
precondition(native.configuration.fetchSubtasks=="on")
var misplaced=parameters
misplaced.removeValue(forKey:"fetchPredefinedGroceryListSections")
var configuration=misplaced["configuration"] as! [String:Any]
configuration["fetchPredefinedGroceryListSections"]=false
misplaced["configuration"]=configuration
let badData=try PropertyListSerialization.data(fromPropertyList:misplaced,format:.binary,options:0)
do {
    _=try PropertyListDecoder().decode(NativeParameters.self,from:badData)
    preconditionFailure("The required flag must be at the parameters root")
} catch DecodingError.keyNotFound(let key,let context) {
    precondition(key.stringValue=="fetchPredefinedGroceryListSections" && context.codingPath.isEmpty)
}
print("native section protocol contract passed")
'''
    result=subprocess.run(['swift','-module-cache-path',str(tmp_path/'swift-cache'),'-e','import Foundation\n'+check],capture_output=True,text=True,timeout=90)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'native section protocol contract passed' in result.stdout
