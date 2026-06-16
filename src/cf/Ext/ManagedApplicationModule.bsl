// MCP test manager bootstrap
// Opens the embedded MCPTestManager data processor when the thin
// client starts in /TESTMANAGER mode. The processor opens the
// WebSocket bridge to the MCP server and waits for commands.
//
// This module is the configuration's ManagedApplicationModule; it is
// already a client-side module — no &НаКлиенте directive here.

#Область ОбработчикиСобытий

Процедура ПриНачалеРаботыСистемы()
	ИмяФайлаЛога = КаталогВременныхФайлов() + "mcp_manager.log";
	ЗаписьЛога = Новый ЗаписьТекста(ИмяФайлаЛога,,,Истина);
	ЗаписьЛога.ЗаписатьСтроку(Формат(ТекущаяДата(),"ДФ=yyyy-MM-ddTHH:mm:ss") + " | ПриНачалеРаботыСистемы: вход");
	Попытка
		ЗаписьЛога.ЗаписатьСтроку(Формат(ТекущаяДата(),"ДФ=yyyy-MM-ddTHH:mm:ss") + " | ПриНачалеРаботыСистемы: получаю форму DataProcessor.MCPTestManager");
		ФормаОбработки = ПолучитьФорму("DataProcessor.MCPTestManager.Form.Форма",,,);
		ЗаписьЛога.ЗаписатьСтроку(Формат(ТекущаяДата(),"ДФ=yyyy-MM-ddTHH:mm:ss") + " | ПриНачалеРаботыСистемы: форма получена, открываю");
		ФормаОбработки.Открыть();
		ЗаписьЛога.ЗаписатьСтроку(Формат(ТекущаяДата(),"ДФ=yyyy-MM-ddTHH:mm:ss") + " | ПриНачалеРаботыСистемы: форма открыта");
	Исключение
		Инфо = ИнформацияОбОшибке();
		ЗаписьЛога.ЗаписатьСтроку(Формат(ТекущаяДата(),"ДФ=yyyy-MM-ddTHH:mm:ss") + " | ПриНачалеРаботыСистемы: ИСКЛЮЧЕНИЕ " + ПодробноеПредставлениеОшибки(Инфо));
	КонецПопытки;
	ЗаписьЛога.Закрыть();
КонецПроцедуры

#КонецОбласти
