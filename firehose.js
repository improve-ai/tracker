'use strict';

const AWS = require('aws-sdk')
const zlib = require('zlib')
const es = require('event-stream')
const s3 = new AWS.S3()
const uuidv4 = require('uuid/v4')
const firehose = new AWS.Firehose()
const lambda = new AWS.Lambda()

const naming = require("./naming.js")
const history = require("./history.js")

// Send the event with the timestamp and project name to firehose
module.exports.sendToFirehose = (projectName, body, receivedAt, log) => {
  body["project_name"] = projectName;
  body["received_at"] = receivedAt.toISOString();
  // FIX timestamp must never be in the future
  if (!body["timestamp"]) {
    body["timestamp"] = body["received_at"];
  }
  if (!body["message_id"]) {
    body["message_id"] = uuidv4()
  }
  let firehoseData = new Buffer(JSON.stringify(body)+'\n')
  consoleTime('firehose',log)
  consoleTime('firehose-create',log)

  let firehosePromise = firehose.putRecord({
    DeliveryStreamName: process.env.FIREHOSE_DELIVERY_STREAM_NAME,
    Record: { 
        Data: firehoseData
    }
  }).promise().then(result => {
    consoleTimeEnd('firehose',log)
    return result
  })
  consoleTimeEnd('firehose-create',log)
  return firehosePromise;
}

module.exports.unpackFirehose = async function(event, context) {

  console.log(`processing s3 event ${JSON.stringify(event)}`)

  if (!event.Records || !event.Records.length > 0 || event.Records.some(record => !record.s3 || !record.s3.bucket || !record.s3.bucket.name || !record.s3.object || !record.s3.object.key)) {
    throw new Error(`WARN: Invalid S3 event ${JSON.stringify(event)}`)
  }

  // list all of the shards so that we can decide which shard to write to
  return history.listSortedShardsByProjectName().then(sortedShardsByProjectName => {
    // s3 only ever includes one record per event, but this spec allows multiple, so multiple we will process.
    return Promise.all(event.Records.map(s3EventRecord => {
      return processFirehoseFile(s3EventRecord.s3.bucket.name, s3EventRecord.s3.object.key, sortedShardsByProjectName)
    }))
  }).then(results => {
    // all of the files have been written and meta incoming files have been touched, dispatch the workers to process
    const lambdaArn = naming.getLambdaFunctionArn("dispatchHistoryShardWorkers", context.invokedFunctionArn)
    console.log(`invoking dispatchHistoryShardWorkers`)

    const params = {
      FunctionName: lambdaArn,
      InvocationType: "Event",
    };
    
    return lambda.invoke(params).promise()
  })
}
 
function processFirehoseFile(s3Bucket, firehoseS3Key, sortedShardsByProjectName) {
  
  let buffersByS3Key = {}
  
  // keep the uuid the same so that events in the same date, project, and shard get mapped to the same key
  const uuidPart = uuidv4()

  return history.processCompressedJsonLines(s3Bucket, firehoseS3Key, eventRecord => {
    if (eventRecord.record_type === "choose") { // DEPRECATED
      console.log(`WARN: skipping choose record`)
      return;
    }

    if (!eventRecord || !eventRecord.project_name) {
      console.log(`WARN: skipping record - no project_name in ${JSON.stringify(eventRecord)}`)
      return;
    }

    if (!eventRecord.timestamp || !naming.isValidDate(eventRecord.timestamp)) {
      console.log(`WARN: skipping record - invalid timestamp in ${JSON.stringify(eventRecord)}`)
      return;
    }

    // client reporting of timestamps in the future are handled in sendToFireHose. This should only happen with some clock skew.
    if (new Date(eventRecord.timestamp) > Date.now()) {
      console.log(`WARN: timestamp in the future ${JSON.stringify(eventRecord)}`)
    }

    let projectName = eventRecord.project_name;

    // delete project_name from requestRecord in case its sensitive
    delete eventRecord.project_name;
    
    if (!naming.isValidProjectName(projectName)) {
      console.log(`WARN: skipping record - invalid project_name, not alphanumeric, underscore, dash, space, period ${JSON.stringify(eventRecord)}`)
      return;
    }
    
    let s3Key
    
    // Handle variants records
    if (eventRecord.type && eventRecord.type === "variants") {
      if (!eventRecord.model) {
        console.log(`WARN: skipping record - missing model for variants type ${JSON.stringify(eventRecord)}`)
        return
      }

      if (!naming.isValidModelName(eventRecord.model)) {
        console.log(`WARN: skipping record - invalid model name, not alphanumeric, underscore, dash, space, period ${JSON.stringify(eventRecord)}`)
        return;
      }
      
      s3Key = naming.getVariantsS3Key(projectName, eventRecord.model, firehoseS3Key)
    } else {
      // user_id is deprecated
      // history_id is not required for variants records
      if (!eventRecord.user_id && !eventRecord.history_id) {
        console.log(`WARN: skipping record - no history_id in ${JSON.stringify(eventRecord)}`)
        return;
      }
      
      if (!eventRecord.history_id) {
        eventRecord.history_id = eventRecord.user_id
      }
  
      // look at the list of available shards and assign the event to one
      // events are also segmented by event date
      s3Key = naming.assignToHistoryS3Key(sortedShardsByProjectName[projectName], projectName, eventRecord, uuidPart)
    }

    let buffers = buffersByS3Key[s3Key]
    if (!buffers) {
      buffers = []
      buffersByS3Key[s3Key] = buffers
    }
    buffers.push(Buffer.from(JSON.stringify(eventRecord)+"\n"))

  }).then(() => {
    return writeRecords(buffersByS3Key)
  })
}

function writeRecords(buffersByS3Key) {
  const promises = []
    
  // write out histories
  for (let [s3Key, buffers] of Object.entries(buffersByS3Key)) {

      console.log(`writing ${buffers.length} records to ${s3Key}`)
      
      // write the data
      let params = {
        Body: zlib.gzipSync(Buffer.concat(buffers)),
        Bucket: process.env.RECORDS_BUCKET,
        Key: s3Key
      }

      promises.push(s3.putObject(params).promise())
      
      // if its a normal history key (not a variants key) write out the incoming meta file indicating this key should be processed
      if (naming.isHistoryS3Key(s3Key)) {
        params = {
          Body: JSON.stringify({ "key": s3Key }),
          Bucket: process.env.RECORDS_BUCKET,
          Key: naming.getIncomingHistoryS3Key(s3Key)
        }
    
        promises.push(s3.putObject(params).promise())
      }
    }

  return Promise.all(promises)
}


function consoleTime(name, shouldLog) {
  if (shouldLog) {
    console.time(name);
  }
}

function consoleTimeEnd(name, shouldLog) {
  if (shouldLog) {
    console.timeEnd(name);
  }
}
