// MCP test manager bootstrap
// Opens the embedded MCPTestManager data processor when the thin
// client starts in /TESTMANAGER mode. The processor opens the
// WebSocket bridge to the MCP server and waits for commands.
//
// This module is the configuration's ManagedApplicationModule; it is
// already a client-side module - no &НаКлиенте directive here.

#Область ОбработчикиСобытий

Процедура ПриНачалеРаботыСистемы()
	ЛогMCP("ПриНачалеРаботыСистемы: вход");
	Попытка
		ЛогMCP("ПриНачалеРаботыСистемы: подключаю отложенное открытие формы MCPTestManager");
		ПодключитьОбработчикОжидания("ОткрытьФормуMCPTestManager", 1, Истина);
		ЛогMCP("ПриНачалеРаботыСистемы: обработчик ожидания подключен");
	Исключение
		Инфо = ИнформацияОбОшибке();
		ЛогMCP("ПриНачалеРаботыСистемы: ИСКЛЮЧЕНИЕ " + ПодробноеПредставлениеОшибки(Инфо));
	КонецПопытки;
КонецПроцедуры

// BSLLS:DeprecatedCurrentDate-on
#КонецОбласти

#Область СлужебныеПроцедурыИФункции
// BSLLS:DeprecatedCurrentDate-off

Процедура ОткрытьФормуMCPTestManager() Экспорт
	Попытка
		ЛогMCP("ОткрытьФормуMCPTestManager: открываю форму Обработка.MCPTestManager.Форма.Форма");
		ОткрытьФорму("Обработка.MCPTestManager.Форма.Форма");
		ЛогMCP("ОткрытьФормуMCPTestManager: ОткрытьФорму вызван");
	Исключение
		Инфо = ИнформацияОбОшибке();
		ЛогMCP("ОткрытьФормуMCPTestManager: ИСКЛЮЧЕНИЕ " + ПодробноеПредставлениеОшибки(Инфо));
	КонецПопытки;
КонецПроцедуры

Процедура ЛогMCP(Сообщение)
	ИмяФайлаЛога = КаталогВременныхФайлов() + "mcp_manager.log";
	ЗаписьЛога = Новый ЗаписьТекста(ИмяФайлаЛога,,,Истина);
	ЗаписьЛога.ЗаписатьСтроку(Формат(ТекущаяДата(), "ДФ=yyyy-MM-ddTHH:mm:ss") + " | " + Сообщение);
	ЗаписьЛога.Закрыть();
КонецПроцедуры
// BSLLS:DeprecatedCurrentDate-on

#КонецОбласти
