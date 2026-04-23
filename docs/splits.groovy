import com.sap.gateway.ip.core.customdev.util.Message
import groovy.json.JsonOutput
import groovy.json.JsonSlurper

def Message processData(Message message) {

    def messageLog = messageLogFactory.getMessageLog(message)
    def props = message.getProperties()
    def body = message.getBody(String) ?: "{}"
    def slurper = new JsonSlurper()
    def payload = slurper.parseText(body)

    def fsmApiBaseUrl     = props.get("fsmApiBaseUrl") as String
    def fsmAccountId      = props.get("fsmAccountId") as String
    def fsmCompanyId      = props.get("fsmCompanyId") as String
    def fsmClientId       = props.get("fsmClientId") as String
    def fsmClientSecret   = props.get("fsmClientSecret") as String
    def fsmTokenUrl       = (props.get("fsmTokenUrl") ?: "https://us.fsm.cloud.sap/api/oauth2/v2/token") as String
    def thresholdMin      = (props.get("thresholdMin") ?: "480").toString().toInteger()
    def splitSizeMin      = (props.get("splitSizeMin") ?: "240").toString().toInteger()
    def childNameSuffix   = (props.get("childNameSuffix") ?: " - Split").toString()

    if (messageLog != null) {
        messageLog.addAttachmentAsString("Step0_IncomingPayload", body?.take(1000), "text/plain")
    }

    message.setHeader("CamelHttpResponseCode", 200)

    // Extract activity ID from webhook payload
    def activityId = payload?.data?.results?.find { it }?.id ?: payload?.data?.id ?: payload?.id ?: payload?.activityId

    if (messageLog != null) {
        messageLog.addAttachmentAsString("Step1_ActivityId", activityId ?: "NULL", "text/plain")
    }

    if (!activityId) {
        message.setBody(JsonOutput.toJson([status: "skipped", reason: "No activity ID found in webhook payload"]))
        return message
    }

    // Step 1: Get OAuth token
    def tokenResp = null
    try {
        def credentials = "${fsmClientId}:${fsmClientSecret}".bytes.encodeBase64().toString()
        def tokenBody = "grant_type=client_credentials&cluster_url=" + URLEncoder.encode(fsmApiBaseUrl, "UTF-8")
        def tokenConn = new URL(fsmTokenUrl).openConnection()
        tokenConn.setRequestMethod("POST")
        tokenConn.setRequestProperty("Content-Type", "application/x-www-form-urlencoded")
        tokenConn.setRequestProperty("Authorization", "Basic ${credentials}")
        tokenConn.setRequestProperty("X-Account-ID", fsmAccountId)
        tokenConn.setConnectTimeout(10000)
        tokenConn.setReadTimeout(10000)
        tokenConn.setDoOutput(true)
        tokenConn.getOutputStream().withWriter("UTF-8") { it << tokenBody }

        int tokenCode = tokenConn.getResponseCode()
        String tokenRespBody = ""
        try { tokenRespBody = tokenConn.getInputStream()?.getText("UTF-8") }
        catch (Exception e) { tokenRespBody = tokenConn.getErrorStream()?.getText("UTF-8") }

        if (messageLog != null) {
            messageLog.addAttachmentAsString("Step2_OAuthResponse", "code=${tokenCode}, body=${tokenRespBody?.take(500)}", "text/plain")
        }
        tokenResp = slurper.parseText(tokenRespBody)
    } catch (Exception e) {
        if (messageLog != null) {
            messageLog.addAttachmentAsString("Step2_OAuthError", "Exception: ${e.getClass().getName()}: ${e.getMessage()}", "text/plain")
        }
        message.setBody(JsonOutput.toJson([status: "error", step: "oauth", reason: e.getMessage()]))
        return message
    }

    if (!tokenResp?.access_token) {
        message.setBody(JsonOutput.toJson([status: "error", step: "oauth", reason: "No access_token in response", tokenResponse: tokenResp?.toString()?.take(500)]))
        return message
    }

    def accessToken = tokenResp.access_token

    // Step 2: GET the activity
    def headers = [
        "Authorization"   : "Bearer ${accessToken}",
        "X-Client-ID"     : fsmClientId,
        "X-Client-Version": "1.0",
        "X-Account-ID"    : fsmAccountId,
        "X-Company-ID"    : fsmCompanyId,
        "Content-Type"    : "application/json",
        "Accept"          : "application/json"
    ]

    def activityUrl = "${fsmApiBaseUrl}/api/data/v4/Activity/${activityId}?dtos=Activity.43"
    def actResp = null
    try {
        def getConn = new URL(activityUrl).openConnection()
        getConn.setRequestMethod("GET")
        getConn.setConnectTimeout(10000)
        getConn.setReadTimeout(10000)
        headers.each { k, v -> if (v != null) getConn.setRequestProperty(k, v.toString()) }

        int getCode = getConn.getResponseCode()
        String getRespBody = ""
        try { getRespBody = getConn.getInputStream()?.getText("UTF-8") }
        catch (Exception e) { getRespBody = getConn.getErrorStream()?.getText("UTF-8") }

        if (messageLog != null) {
            messageLog.addAttachmentAsString("Step3_GetActivityResponse", "code=${getCode}, body=${getRespBody?.take(2000)}", "text/plain")
        }

        if (getCode >= 200 && getCode < 300) {
            def parsed = slurper.parseText(getRespBody)
            actResp = parsed?.data?.getAt(0)?.activity ?: parsed
        }
    } catch (Exception e) {
        if (messageLog != null) {
            messageLog.addAttachmentAsString("Step3_GetActivityError", "Exception: ${e.getClass().getName()}: ${e.getMessage()}", "text/plain")
        }
        message.setBody(JsonOutput.toJson([status: "error", step: "getActivity", reason: e.getMessage()]))
        return message
    }

    if (!actResp) {
        message.setBody(JsonOutput.toJson([status: "error", step: "getActivity", reason: "Failed to GET activity", url: activityUrl]))
        return message
    }

    // Duration check
    def totalDuration = actResp.durationInMinutes ?: 0
    if (totalDuration instanceof String) totalDuration = totalDuration.toInteger()

    if (messageLog != null) {
        messageLog.addAttachmentAsString("Step4_DurationCheck",
            "durationInMinutes=${actResp.durationInMinutes}, plannedDurationInMinutes=${actResp.plannedDurationInMinutes}, usingForSplit=${totalDuration}, threshold=${thresholdMin}",
            "text/plain")
    }

    if (totalDuration <= thresholdMin) {
        message.setBody(JsonOutput.toJson([status: "skipped", reason: "Duration ${totalDuration} min <= threshold ${thresholdMin} min", activityId: activityId]))
        return message
    }

    // Step 3: Calculate splits
    def numChildren = Math.ceil(totalDuration / splitSizeMin).toInteger()
    def remainder   = totalDuration % splitSizeMin
    def results     = []

    // ─────────────────────────────────────────────────────────────────────
    // FIX 1: Resolve serviceProduct by matching activity subject to FSM
    //        Item name. Always do the lookup — no fallback to actResp.serviceProduct.
    //
    //  BUGS FIXED vs old code:
    //   a) Parse path was data[0].item.id / data[0].id  →  must be data[0].i.id
    //      (FSM query alias is the selector letter 'i', not the entity name)
    //   b) URL had account/company as query params  →  causes invalid_request
    //      They must only appear as X-Account-ID / X-Company-ID headers
    // ─────────────────────────────────────────────────────────────────────
    def actSubject       = actResp.subject ?: ""
    def serviceProductId = null

    if (actSubject) {
        try {
            // account & company go in headers only — NOT in the URL
            def itemQueryUrl = "${fsmApiBaseUrl}/api/query/v1?query=" +
                URLEncoder.encode("SELECT i.id FROM Item i WHERE i.name = '${actSubject}' LIMIT 1", "UTF-8") +
                "&dtos=Item.24"

            def itemConn = new URL(itemQueryUrl).openConnection()
            itemConn.setRequestMethod("GET")
            itemConn.setConnectTimeout(10000)
            itemConn.setReadTimeout(10000)
            headers.each { k, v -> if (v != null) itemConn.setRequestProperty(k, v.toString()) }

            int itemCode = itemConn.getResponseCode()
            String itemRespBody = ""
            try { itemRespBody = itemConn.getInputStream()?.getText("UTF-8") }
            catch (Exception e2) { itemRespBody = itemConn.getErrorStream()?.getText("UTF-8") }

            if (messageLog != null) {
                messageLog.addAttachmentAsString("Step4b_ItemLookup",
                    "subject=${actSubject}, code=${itemCode}, body=${itemRespBody?.take(500)}", "text/plain")
            }

            if (itemCode >= 200 && itemCode < 300) {
                def itemParsed = slurper.parseText(itemRespBody)
                // FIXED: correct parse path is data[0].i.id  (alias = 'i')
                serviceProductId = itemParsed?.data?.getAt(0)?.i?.id
            }
        } catch (Exception e) {
            if (messageLog != null) {
                messageLog.addAttachmentAsString("Step4b_ItemLookupError",
                    "Exception: ${e.getClass().getName()}: ${e.getMessage()}", "text/plain")
            }
        }
    }

    if (messageLog != null) {
        messageLog.addAttachmentAsString("Step4c_ServiceProduct",
            "subject=${actSubject}, resolvedServiceProductId=${serviceProductId ?: 'NOT FOUND'}", "text/plain")
    }

    // ─────────────────────────────────────────────────────────────────────
    // FIX 2: Check S/4HANA origin via Service Call externalId.
    //        LOG ONLY — never block the split. Split always proceeds.
    //        Step4d is purely informational for traceability.
    // ─────────────────────────────────────────────────────────────────────
    def serviceCallRef = actResp.serviceCall
    def serviceCallId  = (serviceCallRef instanceof Map) ? serviceCallRef?.id : serviceCallRef
    def scExternalId   = null

    if (serviceCallId) {
        try {
            def scUrl  = "${fsmApiBaseUrl}/api/data/v4/ServiceCall/${serviceCallId}?dtos=ServiceCall.26"
            def scConn = new URL(scUrl).openConnection()
            scConn.setRequestMethod("GET")
            scConn.setConnectTimeout(10000)
            scConn.setReadTimeout(10000)
            headers.each { k, v -> if (v != null) scConn.setRequestProperty(k, v.toString()) }

            int scCode = scConn.getResponseCode()
            String scRespBody = ""
            try { scRespBody = scConn.getInputStream()?.getText("UTF-8") }
            catch (Exception e2) { scRespBody = scConn.getErrorStream()?.getText("UTF-8") }

            if (scCode >= 200 && scCode < 300) {
                def scParsed = slurper.parseText(scRespBody)
                def scResp   = scParsed?.data?.getAt(0)?.serviceCall ?: scParsed
                scExternalId = scResp?.externalId
            }

            if (messageLog != null) {
                messageLog.addAttachmentAsString("Step4d_S4OriginCheck",
                    "serviceCallId=${serviceCallId}, externalId=${scExternalId ?: 'NOT SET - not from S4 or field missing'}, proceeding with split anyway",
                    "text/plain")
            }
        } catch (Exception e) {
            if (messageLog != null) {
                messageLog.addAttachmentAsString("Step4d_S4OriginCheck",
                    "Could not fetch ServiceCall ${serviceCallId}: ${e.getMessage()} — proceeding with split anyway",
                    "text/plain")
            }
        }
    } else {
        if (messageLog != null) {
            messageLog.addAttachmentAsString("Step4d_S4OriginCheck",
                "No serviceCall linked to activity — proceeding with split anyway",
                "text/plain")
        }
    }

    // Step 4: PUT parent activity — update duration AND serviceProduct
    try {
        def putUrl     = "${fsmApiBaseUrl}/api/data/v4/Activity/${activityId}?dtos=Activity.43"
        def updateBody = new java.util.LinkedHashMap(actResp)
        updateBody.durationInMinutes        = splitSizeMin
        updateBody.plannedDurationInMinutes = splitSizeMin
        if (serviceProductId != null) updateBody.serviceProduct = serviceProductId

        def putBody = JsonOutput.toJson(updateBody)
        def putConn = new URL(putUrl).openConnection()
        putConn.setRequestMethod("PUT")
        putConn.setConnectTimeout(10000)
        putConn.setReadTimeout(10000)
        headers.each { k, v -> if (v != null) putConn.setRequestProperty(k, v.toString()) }
        putConn.setDoOutput(true)
        putConn.getOutputStream().withWriter("UTF-8") { it << putBody }

        int putCode = putConn.getResponseCode()
        String putRespBody = ""
        try { putRespBody = putConn.getInputStream()?.getText("UTF-8") }
        catch (Exception e2) { putRespBody = putConn.getErrorStream()?.getText("UTF-8") }

        if (messageLog != null) {
            messageLog.addAttachmentAsString("Step5_PutParent",
                "code=${putCode}, body=${putRespBody?.take(1000)}", "text/plain")
        }
        results << [action: "putParent", activityId: activityId, newDuration: splitSizeMin,
                    newPlannedDuration: splitSizeMin, httpCode: putCode,
                    success: (putCode >= 200 && putCode < 300)]
    } catch (Exception e) {
        if (messageLog != null) {
            messageLog.addAttachmentAsString("Step5_PutParentError",
                "Exception: ${e.getClass().getName()}: ${e.getMessage()}", "text/plain")
        }
        results << [action: "putParent", activityId: activityId, success: false, error: e.getMessage()]
    }

    // Step 5: Create child activities
    def baseSubject       = actResp.subject ?: actResp.code ?: "Activity"
    def activityType      = actResp.type
    def responsiblePerson = actResp.responsiblePerson
    def postUrl           = "${fsmApiBaseUrl}/api/data/v4/Activity?dtos=Activity.43"

    for (int i = 1; i < numChildren; i++) {
        def thisDuration = (i == numChildren - 1 && remainder > 0) ? remainder : splitSizeMin

        def childBody = [
            subject                 : "${baseSubject}${childNameSuffix} ${i + 1}",
            durationInMinutes       : thisDuration,
            plannedDurationInMinutes: thisDuration,
            object                  : actResp.object,
            address                 : actResp.address,
            equipment               : actResp.equipment,
            startDateTime           : actResp.startDateTime,
            endDateTime             : actResp.endDateTime
        ]

        if (serviceCallRef    != null) childBody.serviceCall        = serviceCallRef
        if (activityType      != null) childBody.type               = activityType
        if (responsiblePerson != null) childBody.responsiblePerson  = responsiblePerson
        if (serviceProductId  != null) childBody.serviceProduct     = serviceProductId

        childBody = childBody.findAll { k, v -> v != null }

        try {
            def postConn = new URL(postUrl).openConnection()
            postConn.setRequestMethod("POST")
            postConn.setConnectTimeout(10000)
            postConn.setReadTimeout(10000)
            headers.each { k, v -> if (v != null) postConn.setRequestProperty(k, v.toString()) }
            postConn.setDoOutput(true)

            def childJson = JsonOutput.toJson(childBody)
            postConn.getOutputStream().withWriter("UTF-8") { it << childJson }

            int postCode = postConn.getResponseCode()
            String postRespBody = ""
            try { postRespBody = postConn.getInputStream()?.getText("UTF-8") }
            catch (Exception e2) { postRespBody = postConn.getErrorStream()?.getText("UTF-8") }

            if (messageLog != null) {
                messageLog.addAttachmentAsString("Step6_CreateChild_${i + 1}",
                    "code=${postCode}, sentBody=${childJson?.take(500)}, resp=${postRespBody?.take(1000)}", "text/plain")
            }

            def postParsed = null
            try { postParsed = slurper.parseText(postRespBody) } catch (Exception ep) {}

            results << [action: "createChild", index: i + 1, subject: childBody.subject,
                        duration: thisDuration, plannedDuration: thisDuration,
                        serviceProduct: serviceProductId, httpCode: postCode,
                        success: (postCode >= 200 && postCode < 300), childId: postParsed?.id]
        } catch (Exception e) {
            if (messageLog != null) {
                messageLog.addAttachmentAsString("Step6_CreateChild_${i + 1}_Error",
                    "Exception: ${e.getClass().getName()}: ${e.getMessage()}", "text/plain")
            }
            results << [action: "createChild", index: i + 1, success: false, error: e.getMessage()]
        }
    }

    def response = [
        status                : "completed",
        originalActivityId    : activityId,
        totalDuration         : totalDuration,
        threshold             : thresholdMin,
        splitSize             : splitSizeMin,
        numberOfParts         : numChildren,
        s4ExternalId          : scExternalId,
        resolvedServiceProduct: serviceProductId,
        results               : results
    ]

    message.setBody(JsonOutput.toJson(response))
    return message
}