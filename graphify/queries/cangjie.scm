; Cangjie -- graphify tags query (see graphify/extractors/tags.py for the capture
; vocabulary). Extends the grammar's own tags.scm with enums, properties, `extend`,
; supertypes, imports and calls.

; ── definitions ──
(classDefinition (className) @definition.class)
(interfaceDefinition (interfaceName) @definition.interface)
(structDefinition (structName) @definition.struct)
(enumDefinition (enumName) @definition.enum)
(functionDefinition (funcName) @definition.function)
(propertyDefinition (propertyName) @definition.method)
(macroDefinition (macroName) @definition.macro)
(typeAlias (typeAliasName) @definition.type)
; `extend Foo { ... }` adds members to Foo; it maps onto Foo's own node.
(extendDefinition (extendType (identifier) @name)) @definition.extension

; ── supertypes ──
; A class's first supertype may be its superclass; the rest are interfaces.
(classDefinition (className) . (superOrInterface) @name @reference.class)
(classDefinition (typeParameters) . (superOrInterface) @name @reference.class)
(classDefinition (superOrInterface) (superOrInterface) @name @reference.implementation)
(interfaceDefinition (superOrInterface) @name @reference.class)
(structDefinition (superOrInterface) @name @reference.implementation)
(enumDefinition (superOrInterface) @name @reference.implementation)
(extendDefinition (superOrInterface) @name @reference.implementation)

; ── imports ──
(importList (packageFull packageName: (_) @name)) @reference.import
(importList (packageAlias packageName: (_) @name)) @reference.import
(importList (subGroupOfPackage packageName: (_) @name)) @reference.import
(importList (scoped_identifier) @name) @reference.import
(importList (identifier) @name) @reference.import

; ── calls ──
(postfixExpression
  (atomicVariable (varBindingPattern) @name)
  (callSuffix)) @reference.call
(postfixExpression
  (postfixExpression
    (_) @receiver
    (fieldAccess (atomicVariable (varBindingPattern) @name)))
  (callSuffix)) @reference.call
